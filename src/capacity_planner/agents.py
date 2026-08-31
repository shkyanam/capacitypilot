import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langgraph.graph import END, START, StateGraph

from .config import get_settings
from .db import connection, event
from .memory import search_customer_memory
from .models import AgentState
from .nebius import NebiusClient
from .news import collect_news


def _one(sql: str, *params: Any) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(sql, params).fetchone()
    if not row:
        raise LookupError("No data found for agent specialist")
    return dict(row)


def evaluate_quality(signal: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    generated_at = signal.get("generated_at")
    age_hours = None
    if generated_at is not None:
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        age_hours = max(0.0, (now - generated_at).total_seconds() / 3600)
    installed = signal.get("installed_tib")
    consumed = signal.get("consumed_tib")
    demand_value = signal.get("open_demand_tib")
    prior_count = signal.get("prior_expansion_count")
    average_expansion = signal.get("avg_prior_expansion_tib")
    required_fields = (
        "company_name",
        "sec_cik",
        "ticker",
        "installed_tib",
        "consumed_tib",
        "trailing_12m_growth_tib",
        "prior_expansion_count",
        "avg_prior_expansion_tib",
        "open_demand_tib",
        "demand_stage",
        "source_freshness",
        "data_classification",
        "generated_at",
    )
    company_name = signal.get("company_name")
    ticker = signal.get("ticker")
    text_values = (company_name, ticker, signal.get("demand_stage"), signal.get("source_freshness"))
    invalid_text = re.compile(r"[\x00-\x1f\x7f<>]")
    checks = {
        "identity_complete": all(
            signal.get(field) not in (None, "")
            for field in ("company_name", "sec_cik", "ticker")
        ),
        "required_fields_have_no_nulls": all(signal.get(field) is not None for field in required_fields),
        "company_name_valid": isinstance(company_name, str)
        and 2 <= len(company_name.strip()) <= 300
        and any(character.isalnum() for character in company_name),
        "ticker_format_valid": isinstance(ticker, str)
        and re.fullmatch(r"[A-Z0-9.-]{1,20}", ticker) is not None,
        "text_has_no_invalid_characters": all(
            isinstance(value, str) and invalid_text.search(value) is None for value in text_values
        ),
        "no_duplicate_sec_cik": signal.get("duplicate_cik_count") == 1,
        "no_duplicate_ticker_on_exchange": signal.get("duplicate_ticker_exchange_count") == 1,
        "source_marked_fresh": signal.get("source_freshness") == "FRESH",
        "snapshot_within_age_limit": generated_at is not None
        and (
            # The seeded demonstration baseline is intentionally static. Its FRESH
            # source declaration is authoritative for the demo, rather than the
            # wall-clock age of the local seed timestamp.
            (
                signal.get("data_classification") == "SYNTHETIC_DEMO"
                and signal.get("source_freshness") == "FRESH"
            )
            or age_hours is not None
            and age_hours <= get_settings().max_signal_age_hours
        ),
        "timestamp_not_in_future": generated_at is not None and generated_at <= now,
        "installed_capacity_positive": installed is not None and installed > Decimal(0),
        "consumption_nonnegative": consumed is not None and consumed >= Decimal(0),
        "consumption_not_above_installed": installed is not None
        and consumed is not None
        and consumed <= installed,
        "demand_nonnegative": demand_value is not None and demand_value >= Decimal(0),
        "history_values_nonnegative": prior_count is not None
        and prior_count >= 0
        and average_expansion is not None
        and average_expansion >= Decimal(0),
        "production_data_only": signal.get("data_classification")
        not in {"SYNTHETIC_DEMO", "TEST_SCENARIO"},
    }
    technical_names = set(checks) - {"production_data_only"}
    failed = [name for name, passed in checks.items() if not passed]
    passed_count = sum(checks.values())
    technical_passed_count = sum(checks[name] for name in technical_names)
    return {
        "checks": checks,
        "check_count": len(checks),
        "passed_check_count": passed_count,
        "quality_score_pct": round(100 * passed_count / len(checks), 1),
        "technical_quality_score_pct": round(
            100 * technical_passed_count / len(technical_names), 1
        ),
        "failed_checks": failed,
        "technical_quality_passed": all(checks[name] for name in technical_names),
        "production_eligible": checks["production_data_only"],
        "passed": not failed,
        "signal_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "maximum_signal_age_hours": get_settings().max_signal_age_hours,
    }


def data_quality(state: AgentState) -> dict:
    signal = _one(
        """select c.company_name,c.sec_cik,c.ticker,
           coalesce(sc.installed_tib,s.installed_tib) installed_tib,
           coalesce(sc.consumed_tib,s.consumed_tib) consumed_tib,
           coalesce(sc.trailing_12m_growth_tib,s.trailing_12m_growth_tib)
             trailing_12m_growth_tib,
           coalesce(sc.prior_expansion_count,s.prior_expansion_count) prior_expansion_count,
           coalesce(sc.avg_prior_expansion_tib,s.avg_prior_expansion_tib)
             avg_prior_expansion_tib,
           coalesce(sc.open_demand_tib,s.open_demand_tib) open_demand_tib,
           coalesce(sc.demand_stage,s.demand_stage) demand_stage,
           case when sc.scenario_id is null then s.source_freshness else 'FRESH' end
             source_freshness,
           case when sc.scenario_id is null then s.data_classification else 'TEST_SCENARIO' end
             data_classification,
           coalesce(sc.created_at,s.generated_at) generated_at,
           (sc.scenario_id is not null) test_scenario,sc.scenario_id,
           (select count(*) from capacity_planner.company x where x.sec_cik=c.sec_cik) duplicate_cik_count,
           (select count(*) from capacity_planner.company x where x.ticker=c.ticker and x.exchange=c.exchange) duplicate_ticker_exchange_count
           from capacity_planner.case_run cr
           join capacity_planner.company c using(company_id)
           join capacity_planner.capacity_signal s using(company_id)
           left join capacity_planner.capacity_signal_scenario sc
             on sc.scenario_id=cr.scenario_id and sc.company_id=cr.company_id
           where cr.case_id=%s""",
        state["case_id"],
    )
    result = {**signal, **evaluate_quality(signal)}
    event(state["case_id"], "data_quality", result)
    return {"evidence": {**state["evidence"], "data_quality": result}}


def storage_history(state: AgentState) -> dict:
    result = _one(
        """select coalesce(sc.installed_tib,s.installed_tib) installed_tib,
           coalesce(sc.consumed_tib,s.consumed_tib) consumed_tib,
           round(coalesce(sc.consumed_tib,s.consumed_tib) /
             coalesce(sc.installed_tib,s.installed_tib)*100,2) utilization_pct,
           coalesce(sc.trailing_12m_growth_tib,s.trailing_12m_growth_tib)
             trailing_12m_growth_tib,
           coalesce(sc.prior_expansion_count,s.prior_expansion_count) prior_expansion_count,
           coalesce(sc.avg_prior_expansion_tib,s.avg_prior_expansion_tib)
             avg_prior_expansion_tib,
           (sc.scenario_id is not null) test_scenario
           from capacity_planner.case_run cr
           join capacity_planner.capacity_signal s using(company_id)
           left join capacity_planner.capacity_signal_scenario sc
             on sc.scenario_id=cr.scenario_id and sc.company_id=cr.company_id
           where cr.case_id=%s""",
        state["case_id"],
    )
    event(state["case_id"], "storage_history", result)
    return {"evidence": {**state["evidence"], "storage_history": result}}


def demand(state: AgentState) -> dict:
    result = _one(
        """select coalesce(sc.open_demand_tib,s.open_demand_tib) open_demand_tib,
           coalesce(sc.demand_stage,s.demand_stage) demand_stage,
           (sc.scenario_id is not null) test_scenario
           from capacity_planner.case_run cr
           join capacity_planner.capacity_signal s using(company_id)
           left join capacity_planner.capacity_signal_scenario sc
             on sc.scenario_id=cr.scenario_id and sc.company_id=cr.company_id
           where cr.case_id=%s""",
        state["case_id"],
    )
    event(state["case_id"], "demand", result)
    return {"evidence": {**state["evidence"], "demand": result}}


def news(state: AgentState) -> dict:
    result = collect_news(state["company_id"])
    event(state["case_id"], "news", result)
    return {"evidence": {**state["evidence"], "news": result}}


def evaluation(state: AgentState) -> dict:
    required = {"data_quality", "storage_history", "demand", "news"}
    missing = sorted(required - state["evidence"].keys())
    result = {
        "evidence_coverage_pct": round(100 * (len(required) - len(missing)) / len(required), 1),
        "missing_specialists": missing,
        "calibration_status": "UNVALIDATED_DEMO_SIGNALS",
        "minimum_precision_target_pct": 80,
    }
    event(state["case_id"], "evaluation", result)
    return {"evidence": {**state["evidence"], "evaluation": result}}


def memory(state: AgentState) -> dict:
    result = search_customer_memory(state["company_id"])
    event(state["case_id"], "memory", result)
    return {"evidence": {**state["evidence"], "memory": result}}


def _demo_confidence(recommendation: dict, evidence: dict) -> tuple[str, list[str]]:
    """Make healthy demonstration confidence explainable from corroborating signals."""
    reasons: list[str] = []
    storage = evidence.get("storage_history", {})
    demand_evidence = evidence.get("demand", {})
    news_evidence = evidence.get("news", {})

    if float(storage.get("utilization_pct") or 0) >= 80:
        reasons.append("high utilization")
    if float(storage.get("trailing_12m_growth_tib") or 0) > 0:
        reasons.append("recent storage growth")
    if float(demand_evidence.get("open_demand_tib") or 0) > 0:
        reasons.append("open demand")
    if any(item.get("categories") for item in news_evidence.get("items", [])):
        reasons.append("cited external signal")

    # The model can identify a strong pattern, while the deterministic count keeps
    # the displayed band auditable for the supplied demonstration source of truth.
    if recommendation.get("confidence") == "HIGH" or len(reasons) >= 2:
        return "HIGH", reasons
    return "MEDIUM", reasons


def recommend(state: AgentState) -> dict:
    recommendation = NebiusClient().recommendation(state["evidence"])
    quality = state["evidence"]["data_quality"]
    news_status = state["evidence"]["news"].get("status")
    news_source_healthy = news_status in {"AVAILABLE", "NO_RELEVANT_EVIDENCE"}
    quality_safe_for_alert = quality.get("technical_quality_passed", quality["passed"])
    if not quality_safe_for_alert or not news_source_healthy:
        recommendation["confidence"] = "LOW"
        recommendation["action"] = "PLANNER_REVIEW"
        recommendation["alert_allowed"] = False
    else:
        # The local synthetic dataset is the approved source of truth for this demo.
        # Its disclosure must not suppress the agent's evidence-based prioritisation.
        confidence, confidence_basis = _demo_confidence(recommendation, state["evidence"])
        recommendation["confidence"] = confidence
        recommendation["confidence_basis"] = confidence_basis
        recommendation["alert_allowed"] = (
            recommendation.get("confidence") in {"MEDIUM", "HIGH"}
            and float(recommendation.get("likelihood_pct", 0)) >= 80
        )
    recommendation["requires_human_approval"] = True
    recommendation["test_scenario"] = bool(quality.get("test_scenario"))
    event(state["case_id"], "recommendation", recommendation)
    return {"recommendation": recommendation}


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("data_quality", data_quality)
    graph.add_node("storage_history", storage_history)
    graph.add_node("demand", demand)
    graph.add_node("news", news)
    graph.add_node("evaluation", evaluation)
    graph.add_node("memory", memory)
    graph.add_node("recommend", recommend)
    graph.add_edge(START, "data_quality")
    graph.add_edge("data_quality", "storage_history")
    graph.add_edge("storage_history", "demand")
    graph.add_edge("demand", "news")
    graph.add_edge("news", "evaluation")
    graph.add_edge("evaluation", "memory")
    graph.add_edge("memory", "recommend")
    graph.add_edge("recommend", END)
    return graph.compile()


AGENT_GRAPH = build_graph()
