import logging
import re
from typing import Any

from .db import connection
from .models import PortfolioQueryPlan
from .nebius import NebiusClient

LOG = logging.getLogger(__name__)

SORT_COLUMNS = {
    "utilization_pct": "utilization_pct",
    "open_demand_tib": "open_demand_tib",
    "annual_growth_tib": "annual_growth_tib",
    "likelihood_pct": "likelihood_pct",
    "suggested_growth_tib": "suggested_growth_tib",
    "company_name": "company_name",
}

REGION_ALIASES = {
    "apac": ["ap-tokyo-1"],
    "emea": ["eu-frankfurt-1", "uk-london-1"],
    "us": ["us-ashburn-1", "us-phoenix-1"],
    "united states": ["us-ashburn-1", "us-phoenix-1"],
}
REGIONS = [
    "ap-tokyo-1",
    "eu-frankfurt-1",
    "uk-london-1",
    "us-ashburn-1",
    "us-phoenix-1",
]


def reservation_audit_plan(question: str) -> dict[str, Any] | None:
    """Recognize reservation audit counts before any general portfolio interpretation."""
    text = question.lower()
    if not re.search(r"\b(reservation|reservations|reserved capacity)\b", text):
        return None
    if not re.search(r"\b(how many|count|number of)\b", text):
        return {"intent": "RESERVATION_AUDIT_UNSUPPORTED"}

    hours = None
    hour_match = re.search(
        r"\b(?:last|past|previous)\s+(\d{1,4})\s*(?:hours?|hrs?|h)\b", text
    )
    day_match = re.search(
        r"\b(?:last|past|previous)\s+(\d{1,3})\s*days?\b", text
    )
    if hour_match:
        hours = min(8760, max(1, int(hour_match.group(1))))
    elif day_match:
        hours = min(8760, max(1, int(day_match.group(1)) * 24))
    elif re.search(r"\b(?:last|past|previous)\s+(?:day|24\s*hours?)\b", text):
        hours = 24
    elif re.search(r"\b(?:last|past|previous)\s+week\b", text):
        hours = 168

    return {
        "intent": "RESERVATION_AUDIT_COUNT",
        "status": "LOCAL_RESERVED",
        "time_window_hours": hours,
        "scope": "ALL_PLANNERS",
    }


def contextual_follow_up_plan(
    question: str, context: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Resolve terse follow-ups from validated session context, never model inference."""
    if not context or not str(context.get("previous_intent", "")).startswith(
        "RESERVATION_AUDIT_"
    ):
        return None
    text = question.lower().strip()
    if re.search(
        r"\b(customers?|utilization|utilisation|demand|growth|likelihood|forecast|news)\b",
        text,
    ):
        return None
    inherited = {
        "status": "LOCAL_RESERVED",
        "time_window_hours": context.get("previous_time_window_hours"),
        "scope": "ALL_PLANNERS",
        "context_inherited": True,
    }
    if re.search(
        r"\b(display|show|list|details?|which|what)\b.*\b(them|all|ones?)\b"
        r"|\b(all of them|show them|list them|which ones|what are they)\b",
        text,
    ):
        return {"intent": "RESERVATION_AUDIT_LIST", **inherited}
    if re.search(r"\b(how many|count|number of)\b", text):
        return {"intent": "RESERVATION_AUDIT_COUNT", **inherited}
    return {"intent": "RESERVATION_AUDIT_UNSUPPORTED", **inherited}


def planner_review_count_plan(question: str) -> PortfolioQueryPlan | None:
    """Route the common planner-review count without depending on an LLM."""
    text = question.lower()
    if not re.search(r"\b(how many|count|number of)\b", text):
        return None
    if not re.search(
        r"\b(?:need|needs|pending)\s+(?:planner\s+)?review\b", text
    ):
        return None
    return PortfolioQueryPlan(
        operation="COUNT",
        planner_states=["NEEDS_REVIEW"],
        explanation="Direct count of customers awaiting planner review.",
    )


def query_reservation_audit(hours: int | None) -> dict[str, Any]:
    """Count committed local reservations from the authoritative audit table."""
    conditions = ["status='LOCAL_RESERVED'"]
    params: list[Any] = []
    if hours is not None:
        conditions.append("created_at >= now()-(%s * interval '1 hour')")
        params.append(hours)
    with connection() as conn:
        row = conn.execute(
            """select count(*) approved_reservations,
                 count(distinct company_id) customers,
                 count(distinct planner_identity) planner_identities,
                 round(coalesce(sum(requested_tib),0),2) reserved_tib,
                 max(created_at) latest_reservation_at
               from capacity_planner.local_capacity_reservation where """
            + " and ".join(conditions),
            params,
        ).fetchone()
    return dict(row)


def query_reservation_details(hours: int | None) -> list[dict[str, Any]]:
    """Return the auditable reservation rows referenced by a count response."""
    conditions = ["r.status='LOCAL_RESERVED'"]
    params: list[Any] = []
    if hours is not None:
        conditions.append("r.created_at >= now()-(%s * interval '1 hour')")
        params.append(hours)
    with connection() as conn:
        rows = conn.execute(
            """select r.reservation_id,c.company_name,c.ticker,r.region,r.qfab,
                 r.requested_tib,r.target_date,r.status,r.planner_identity,
                 r.created_at
               from capacity_planner.local_capacity_reservation r
               join capacity_planner.company c using(company_id)
               where """
            + " and ".join(conditions)
            + " order by r.created_at desc limit 1000",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def evaluate_portfolio_chat_contract() -> dict[str, Any]:
    """Deterministic evals for high-risk transactional chatbot intents."""
    checks = {
        "reservation_count_routes_to_audit": (
            reservation_audit_plan("How many reservations are approved?")["intent"]
            == "RESERVATION_AUDIT_COUNT"
        ),
        "last_24hrs_is_parsed": (
            reservation_audit_plan(
                "How many reservations are approved in the last 24hrs?"
            )["time_window_hours"]
            == 24
        ),
        "last_7_days_is_parsed": (
            reservation_audit_plan("Count reservations in the last 7 days")[
                "time_window_hours"
            ]
            == 168
        ),
        "reservation_topic_never_becomes_portfolio_list": (
            reservation_audit_plan("Show approved reservations")["intent"]
            == "RESERVATION_AUDIT_UNSUPPORTED"
        ),
        "customer_question_remains_portfolio_scope": (
            reservation_audit_plan("How many customers need planner review?") is None
        ),
        "display_all_followup_inherits_reservation_context": (
            contextual_follow_up_plan(
                "Can you display all of them?",
                {
                    "previous_intent": "RESERVATION_AUDIT_COUNT",
                    "previous_time_window_hours": 24,
                },
            )["intent"]
            == "RESERVATION_AUDIT_LIST"
        ),
        "followup_preserves_time_window": (
            contextual_follow_up_plan(
                "Show them",
                {
                    "previous_intent": "RESERVATION_AUDIT_COUNT",
                    "previous_time_window_hours": 24,
                },
            )["time_window_hours"]
            == 24
        ),
        "ambiguous_followup_cannot_become_portfolio_list": (
            contextual_follow_up_plan(
                "Tell me more",
                {
                    "previous_intent": "RESERVATION_AUDIT_COUNT",
                    "previous_time_window_hours": 24,
                },
            )["intent"]
            == "RESERVATION_AUDIT_UNSUPPORTED"
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "passed_checks": len(checks) - len(failed),
        "total_checks": len(checks),
        "failed_checks": failed,
    }


def fallback_query_plan(question: str) -> PortfolioQueryPlan:
    """Interpret common portfolio questions when the external LLM is unavailable."""
    text = question.lower()
    operation = (
        "COUNT"
        if re.search(r"\b(how many|count)\b", text)
        else "SUMMARY"
        if re.search(r"\b(summarize|summary|average|total)\b", text)
        else "LIST"
    )
    if "open demand" in text or "demand" in text:
        sort_by = "open_demand_tib"
    elif "suggested" in text and "growth" in text:
        sort_by = "suggested_growth_tib"
    elif "growth" in text and "likelihood" not in text:
        sort_by = "annual_growth_tib"
    elif "utilization" in text or "utilisation" in text:
        sort_by = "utilization_pct"
    else:
        sort_by = "likelihood_pct"
    limit_match = re.search(r"\b(?:top|first|show|which)\s+(\d{1,2})\b", text)
    limit = min(50, max(1, int(limit_match.group(1)))) if limit_match else 10

    def threshold(metric: str) -> float | None:
        patterns = [
            rf"(?:above|over|at least|greater than|>=)\s*(\d+(?:\.\d+)?)\s*%?\s*{metric}",
            rf"{metric}\s*(?:above|over|at least|greater than|>=)\s*(\d+(?:\.\d+)?)\s*%?",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return float(match.group(1))
        return None

    regions: list[str] = []
    for alias, values in REGION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            regions.extend(values)
    regions.extend(region for region in REGIONS if region in text)
    confidence = [
        value
        for value in ("LOW", "MEDIUM", "HIGH")
        if re.search(
            rf"\b{value.lower()}\s+confidence\b|\bconfidence\s+(?:is\s+)?{value.lower()}\b",
            text,
        )
    ]
    planner_states = []
    state_phrases = {
        "NEEDS_REVIEW": (
            "need review",
            "needs review",
            "need planner review",
            "needs planner review",
            "pending review",
        ),
        "IN_PROGRESS": ("in progress", "running investigation"),
        "REVIEWED": ("reviewed", "planner decided"),
        "MONITORING": ("monitoring", "monitor"),
        "NOT_INVESTIGATED": ("not investigated", "without recommendation"),
    }
    for state, phrases in state_phrases.items():
        if any(phrase in text for phrase in phrases):
            planner_states.append(state)
    return PortfolioQueryPlan(
        operation=operation,
        regions=list(dict.fromkeys(regions)),
        min_utilization_pct=threshold(r"(?:utilization|utilisation)"),
        min_likelihood_pct=threshold(r"(?:likelihood|probability)"),
        min_open_demand_tib=threshold(r"(?:open\s+)?demand"),
        min_annual_growth_tib=threshold(r"(?:annual|12-month)?\s*growth"),
        confidence=confidence,
        planner_states=planner_states,
        sort_by=sort_by,
        limit=limit,
        explanation="Safe built-in interpretation used because Nebius was unavailable.",
    )


def _portfolio_sql(plan: PortfolioQueryPlan) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if plan.customer_search:
        escaped = (
            plan.customer_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        conditions.append("(company_name ilike %s escape '\\' or ticker ilike %s escape '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    filters = [
        (plan.regions, "region = any(%s)"),
        (plan.confidence, "confidence = any(%s)"),
        (plan.demand_stages, "demand_stage = any(%s)"),
        (plan.planner_states, "planner_state = any(%s)"),
    ]
    for values, sql in filters:
        if values:
            conditions.append(sql)
            params.append(values)
    thresholds = [
        (plan.min_utilization_pct, "utilization_pct >= %s"),
        (plan.max_utilization_pct, "utilization_pct <= %s"),
        (plan.min_likelihood_pct, "likelihood_pct >= %s"),
        (plan.max_likelihood_pct, "likelihood_pct <= %s"),
        (plan.min_open_demand_tib, "open_demand_tib >= %s"),
        (plan.min_annual_growth_tib, "annual_growth_tib >= %s"),
    ]
    for value, sql in thresholds:
        if value is not None:
            conditions.append(sql)
            params.append(value)
    where = " where " + " and ".join(conditions) if conditions else ""
    return where, params


PORTFOLIO_CTE = """with portfolio as (
  select c.company_id,c.company_name,c.ticker,c.region,
    round(s.consumed_tib/s.installed_tib*100,2) utilization_pct,
    s.installed_tib,s.consumed_tib,
    s.trailing_12m_growth_tib annual_growth_tib,s.open_demand_tib,s.demand_stage,
    (rec.recommendation->>'likelihood_pct')::numeric likelihood_pct,
    rec.recommendation->>'confidence' confidence,
    (rec.recommendation->>'timing_days')::integer timing_days,
    (rec.recommendation->>'capacity_growth_tib')::numeric suggested_growth_tib,
    rec.updated_at last_recommendation_at,
    case
      when decision.decision is not null then 'REVIEWED'
      when latest.status in ('QUEUED','RUNNING','RETRY') then 'IN_PROGRESS'
      when (rec.recommendation->>'likelihood_pct')::numeric >= 75 then 'NEEDS_REVIEW'
      when rec.recommendation is not null then 'MONITORING'
      else 'NOT_INVESTIGATED'
    end planner_state
  from capacity_planner.company c
  join capacity_planner.capacity_signal s using(company_id)
  left join lateral (
    select cr.status from capacity_planner.case_run cr
    where cr.company_id=c.company_id order by cr.created_at desc limit 1
  ) latest on true
  left join lateral (
    select cr.recommendation,cr.updated_at from capacity_planner.case_run cr
    where cr.company_id=c.company_id and cr.recommendation is not null
    order by cr.created_at desc limit 1
  ) rec on true
  left join lateral (
    select pd.decision from capacity_planner.planner_decision pd
    join capacity_planner.case_run cr using(case_id)
    where cr.company_id=c.company_id order by pd.decided_at desc limit 1
  ) decision on true
)"""


def query_portfolio(plan: PortfolioQueryPlan) -> dict[str, Any]:
    where, params = _portfolio_sql(plan)
    sort_column = SORT_COLUMNS[plan.sort_by]
    direction = plan.sort_direction
    with connection() as conn:
        summary = conn.execute(
            PORTFOLIO_CTE
            + """ select count(*) matching_customers,
              round(avg(utilization_pct),1) average_utilization_pct,
              round(coalesce(sum(open_demand_tib),0),2) total_open_demand_tib,
              round(avg(likelihood_pct),1) average_likelihood_pct,
              round(coalesce(sum(suggested_growth_tib),0),2) total_suggested_growth_tib
              from portfolio"""
            + where,
            params,
        ).fetchone()
        rows = conn.execute(
            PORTFOLIO_CTE
            + " select * from portfolio"
            + where
            + f" order by {sort_column} {direction} nulls last limit %s",
            (*params, plan.limit),
        ).fetchall()
    return {"summary": dict(summary), "rows": [dict(row) for row in rows]}


def answer_portfolio_question(
    question: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    audit_plan = reservation_audit_plan(question) or contextual_follow_up_plan(
        question, context
    )
    if audit_plan:
        if audit_plan["intent"] == "RESERVATION_AUDIT_UNSUPPORTED":
            return {
                "answer": (
                    "I can safely count approved local capacity reservations, but I will not "
                    "reinterpret this transactional question as a customer portfolio search. "
                    "Try: ‘How many reservations were approved in the last 24 hours?’"
                ),
                "rows": [],
                "summary": {},
                "interpreted_as": audit_plan,
                "interpretation_source": "DETERMINISTIC_AUDIT",
            }
        if audit_plan["intent"] == "RESERVATION_AUDIT_LIST":
            rows = query_reservation_details(audit_plan["time_window_hours"])
            window = (
                f" from the last {audit_plan['time_window_hours']} hours"
                if audit_plan["time_window_hours"] is not None
                else ""
            )
            return {
                "answer": (
                    f"Here are all {len(rows):,} approved local capacity reservations"
                    f"{window}."
                ),
                "rows": rows,
                "summary": {"approved_reservations": len(rows)},
                "interpreted_as": audit_plan,
                "interpretation_source": "DETERMINISTIC_AUDIT",
            }
        summary = query_reservation_audit(audit_plan["time_window_hours"])
        count = int(summary.get("approved_reservations") or 0)
        customers = int(summary.get("customers") or 0)
        reserved_tib = float(summary.get("reserved_tib") or 0)
        window = (
            f"the last {audit_plan['time_window_hours']} hours"
            if audit_plan["time_window_hours"] is not None
            else "all recorded time"
        )
        noun = "reservation" if count == 1 else "reservations"
        return {
            "answer": (
                f"CapacityPilot records show {count:,} approved local capacity {noun} "
                f"across {customers:,} customers in {window}, totaling "
                f"{reserved_tib:,.1f} TiB. This count covers all planner identities."
            ),
            "rows": [],
            "summary": summary,
            "interpreted_as": audit_plan,
            "interpretation_source": "DETERMINISTIC_AUDIT",
        }
    review_plan = planner_review_count_plan(question)
    if review_plan:
        result = query_portfolio(review_plan)
        count = int(result["summary"].get("matching_customers") or 0)
        return {
            "answer": f"{count:,} customers currently need planner review.",
            **result,
            "interpreted_as": review_plan.model_dump(mode="json"),
            "interpretation_source": "DETERMINISTIC_PORTFOLIO",
        }
    try:
        plan = NebiusClient().portfolio_query_plan(question)
        interpretation_source = "NEBIUS"
    except Exception as exc:  # noqa: BLE001 - intentional provider fallback.
        LOG.warning("portfolio_chat_llm_fallback error_type=%s", type(exc).__name__)
        plan = fallback_query_plan(question)
        interpretation_source = "SAFE_FALLBACK"
    if plan.operation == "UNSUPPORTED":
        return {
            "answer": (
                "I can answer questions about customer storage utilization, growth, demand, "
                "expansion forecasts, regions, and planner-review status."
            ),
            "rows": [],
            "summary": {},
            "interpreted_as": plan.model_dump(mode="json"),
            "interpretation_source": interpretation_source,
        }
    result = query_portfolio(plan)
    summary = result["summary"]
    count = summary["matching_customers"]
    if not count:
        answer = "I found no customers matching those criteria."
    elif plan.operation == "COUNT":
        answer = f"I found {count:,} customers matching those criteria."
    elif plan.operation == "SUMMARY":
        average_utilization = float(summary["average_utilization_pct"] or 0)
        total_demand = float(summary["total_open_demand_tib"] or 0)
        total_growth = float(summary["total_suggested_growth_tib"] or 0)
        answer = (
            f"Across {count:,} matching customers, average utilization is "
            f"{average_utilization:.1f}%, total open demand is {total_demand:,.1f} TiB, "
            f"and suggested capacity growth totals {total_growth:,.1f} TiB."
        )
    else:
        answer = f"I found {count:,} matching customers and displayed the top {len(result['rows'])}."
    return {
        "answer": answer,
        **result,
        "interpreted_as": plan.model_dump(mode="json"),
        "interpretation_source": interpretation_source,
    }
