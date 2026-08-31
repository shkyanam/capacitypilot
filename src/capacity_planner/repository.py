import argparse
import json
import uuid
from decimal import Decimal

from .config import get_settings
from .db import connection
from .memory_outbox import enqueue_planner_decision
from .portfolio_chat import evaluate_portfolio_chat_contract
from .ui_contracts import evaluate_ui_action_contract
from .web import evaluate_link_contract

PORTFOLIO_QUEUE_LOCK_ID = 724001
SERVICE_VAULT_TENANCY = {
    ("Storage Capacity", "Standard"): "Shared",
    ("Storage Capacity", "High Performance"): "Shared",
    ("Storage Capacity", "Ultra Performance"): "Shared",
    ("Storage Capacity", "System Standard"): "Dedicated",
    ("Storage Capacity", "System Critical"): "Dedicated",
    ("Storage Capacity", "Replication"): "Replicated",
    ("Storage Capacity", "General Purpose"): "Replicated",
}


class CapacityUnavailableError(Exception):
    def __init__(self, details: dict):
        self.details = details
        super().__init__(details["message"])


def create_case(company_id: int) -> dict:
    case_id = uuid.uuid4()
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (company_id,))
        exists = conn.execute(
            "select 1 from capacity_planner.company where company_id=%s", (company_id,)
        ).fetchone()
        if not exists:
            raise LookupError("Company not found")
        active = conn.execute(
            """select * from capacity_planner.case_run
               where company_id=%s and status in ('QUEUED','RUNNING','RETRY')
               order by created_at desc limit 1""",
            (company_id,),
        ).fetchone()
        if active:
            return dict(active)
        row = conn.execute(
            """insert into capacity_planner.case_run(case_id,company_id,status,priority)
               values (%s,%s,'QUEUED',10) returning *""",
            (case_id, company_id),
        ).fetchone()
    return dict(row)


def enqueue_initial_portfolio() -> int:
    """Queue each company exactly once for its initial autonomous baseline."""
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (PORTFOLIO_QUEUE_LOCK_ID,))
        company_rows = conn.execute(
            """select c.company_id from capacity_planner.company c
               where not exists (
                 select 1 from capacity_planner.case_run prior
                 where prior.company_id=c.company_id
               )
               order by c.company_id"""
        ).fetchall()
        for row in company_rows:
            conn.execute(
                """insert into capacity_planner.case_run(case_id,company_id,status,priority)
                   values (%s,%s,'QUEUED',100)""",
                (uuid.uuid4(), row["company_id"]),
            )
    return len(company_rows)


def enqueue_portfolio_refresh(limit: int = 100) -> int:
    """Queue fresh, non-overlapping investigations for a bounded portfolio refresh."""
    if limit < 1:
        raise ValueError("limit must be positive")
    queued = 0
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (PORTFOLIO_QUEUE_LOCK_ID,))
        company_rows = conn.execute(
            """select company_id from capacity_planner.company order by company_id limit %s""",
            (limit,),
        ).fetchall()
        for row in company_rows:
            company_id = row["company_id"]
            active = conn.execute(
                """select 1 from capacity_planner.case_run
                   where company_id=%s and status in ('QUEUED','RUNNING','RETRY')
                   limit 1""",
                (company_id,),
            ).fetchone()
            if active:
                continue
            conn.execute(
                """insert into capacity_planner.case_run(case_id,company_id,status,priority)
                   values (%s,%s,'QUEUED',10)""",
                (uuid.uuid4(), company_id),
            )
            queued += 1
    return queued


def refresh_portfolio_main() -> None:
    """CLI entry point for a deliberate one-time portfolio reinvestigation."""
    parser = argparse.ArgumentParser(description="Queue fresh CapacityPilot investigations")
    parser.add_argument("--limit", type=int, default=100, help="Customer count to refresh")
    args = parser.parse_args()
    queued = enqueue_portfolio_refresh(args.limit)
    print(f"Queued {queued} of the first {args.limit} customers for fresh investigation")


def portfolio_status() -> dict:
    with connection() as conn:
        totals = conn.execute(
            """select
                 (select count(*) from capacity_planner.company) total_companies,
                 count(distinct company_id) filter (where recommendation is not null)
                   scored_companies,
                 max(updated_at) filter (where recommendation is not null) last_refresh_at
               from capacity_planner.case_run"""
        ).fetchone()
        status_rows = conn.execute(
            """select status,count(*) case_count from capacity_planner.case_run
               where status in ('QUEUED','RUNNING','RETRY','FAILED')
               group by status"""
        ).fetchall()
    total = totals["total_companies"]
    scored = totals["scored_companies"]
    statuses = {row["status"]: row["case_count"] for row in status_rows}
    active = sum(statuses.get(name, 0) for name in ("QUEUED", "RUNNING", "RETRY"))
    return {
        "total_companies": total,
        "scored_companies": scored,
        "remaining_companies": max(0, total - scored),
        "active_cases": active,
        "failed_cases": statuses.get("FAILED", 0),
        "case_statuses": statuses,
        "baseline_complete": total > 0 and scored == total,
        "last_refresh_at": totals["last_refresh_at"],
    }


def quality_eval_status() -> dict:
    specialist_nodes = (
        "data_quality",
        "storage_history",
        "demand",
        "news",
        "evaluation",
        "memory",
        "recommendation",
    )
    with connection() as conn:
        quality = conn.execute(
            """select count(*) total_runs,
                 round(avg((payload->>'quality_score_pct')::numeric),1)
                   average_quality_pct,
                 round(avg((payload->>'technical_quality_score_pct')::numeric),1)
                   average_technical_quality_pct,
                 count(*) filter (where
                   coalesce((payload->>'technical_quality_passed')::boolean,false))
                   technical_passed_runs,
                 count(*) filter (where
                   coalesce((payload->>'production_eligible')::boolean,false))
                   production_eligible_runs,
                 max(created_at) last_run_at
               from capacity_planner.case_event
               where event_type='data_quality'"""
        ).fetchone()
        quality_checks = conn.execute(
            """select check_item.key check_name,count(*) run_count,
                 count(*) filter (where (check_item.value #>> '{}')::boolean) passed_count,
                 round(100*count(*) filter (
                   where (check_item.value #>> '{}')::boolean)/count(*),1) pass_rate_pct
               from capacity_planner.case_event ce
               cross join lateral jsonb_each(ce.payload->'checks') check_item
               where ce.event_type='data_quality'
               group by check_item.key order by pass_rate_pct,check_item.key"""
        ).fetchall()
        recent_quality = conn.execute(
            """select ce.case_id,c.company_name,c.ticker,cr.status,
                 (cr.scenario_id is not null) planning_simulation,
                 (ce.payload->>'quality_score_pct')::numeric quality_score_pct,
                 (ce.payload->>'technical_quality_score_pct')::numeric
                   technical_quality_score_pct,
                 coalesce((ce.payload->>'production_eligible')::boolean,false)
                   production_eligible,
                 ce.payload->'failed_checks' failed_checks,ce.created_at
               from capacity_planner.case_event ce
               join capacity_planner.case_run cr using(case_id)
               join capacity_planner.company c using(company_id)
               where ce.event_type='data_quality'
               order by ce.created_at desc limit 25"""
        ).fetchall()
        eval_summary = conn.execute(
            """select count(*) evaluation_runs,
                 round(avg((payload->>'evidence_coverage_pct')::numeric),1)
                   average_evidence_coverage_pct,
                 count(*) filter (where
                   (payload->>'evidence_coverage_pct')::numeric=100) full_coverage_runs,
                 max(created_at) last_evaluation_at
               from capacity_planner.case_event
               where event_type='evaluation'"""
        ).fetchone()
        missing_specialists = conn.execute(
            """select missing.value missing_specialist,count(*) missing_count
               from capacity_planner.case_event ce
               cross join lateral jsonb_array_elements_text(
                 ce.payload->'missing_specialists') missing(value)
               where ce.event_type='evaluation'
               group by missing.value order by missing_count desc,missing.value"""
        ).fetchall()
        outcomes = conn.execute(
            """select count(*) labeled,
                 count(*) filter (where o.expanded and
                   (r.recommendation->>'likelihood_pct')::numeric >= 80) true_positive,
                 count(*) filter (where
                   (r.recommendation->>'likelihood_pct')::numeric >= 80)
                   predicted_positive
               from capacity_planner.prediction_outcome o
               join capacity_planner.case_run r using(case_id)"""
        ).fetchone()
        orchestration = conn.execute(
            """select count(*) total_cases,
                 count(*) filter (where status in ('COMPLETE','REVIEW_REQUIRED'))
                   completed_cases,
                 count(*) filter (where status='FAILED') failed_cases,
                 count(*) filter (where status='QUEUED') queued_cases,
                 count(*) filter (where status='RUNNING') running_cases,
                 count(*) filter (where status='RETRY') retry_cases,
                 round(avg(attempt_count),2) average_attempts,
                 round(avg(extract(epoch from (updated_at-created_at))),1)
                   average_duration_seconds,
                 max(updated_at) last_activity_at
               from capacity_planner.case_run"""
        ).fetchone()
        node_counts = conn.execute(
            """select event_type,count(distinct case_id) case_count,
                 max(created_at) last_event_at
               from capacity_planner.case_event
               where event_type=any(%s)
               group by event_type order by event_type""",
            (list(specialist_nodes),),
        ).fetchall()
        failures = conn.execute(
            """select cr.case_id,c.company_name,c.ticker,cr.attempt_count,
                 cr.last_error,cr.updated_at
               from capacity_planner.case_run cr
               join capacity_planner.company c using(company_id)
               where cr.status='FAILED'
               order by cr.updated_at desc limit 20"""
        ).fetchall()
        observability = conn.execute(
            """select
                 count(*) filter (where created_at >= now()-interval '24 hours')
                   cases_started_24h,
                 count(*) filter (where updated_at >= now()-interval '24 hours'
                   and status in ('COMPLETE','REVIEW_REQUIRED')) cases_completed_24h,
                 count(*) filter (where updated_at >= now()-interval '24 hours'
                   and status='FAILED') cases_failed_24h,
                 count(*) filter (where attempt_count > 1) retried_cases,
                 round(avg(extract(epoch from (updated_at-created_at))) filter (
                   where status in ('COMPLETE','REVIEW_REQUIRED','FAILED')),1)
                   average_terminal_latency_seconds,
                 round((percentile_cont(0.95) within group (order by
                   extract(epoch from (updated_at-created_at))) filter (
                   where status in ('COMPLETE','REVIEW_REQUIRED','FAILED')))::numeric,1)
                   p95_terminal_latency_seconds,
                 round(extract(epoch from (now()-min(created_at) filter (
                   where status in ('QUEUED','RETRY'))))::numeric,1)
                   oldest_queue_age_seconds,
                 count(*) filter (where status='RUNNING' and locked_at <
                   now()-(%s * interval '1 minute')) stale_running_cases,
                 max(updated_at) last_observed_at
               from capacity_planner.case_run""",
            (get_settings().stale_case_minutes,),
        ).fetchone()
        hourly_throughput = conn.execute(
            """select date_trunc('hour',updated_at) activity_hour,
                 count(*) filter (where status in ('COMPLETE','REVIEW_REQUIRED')) completed,
                 count(*) filter (where status='FAILED') failed
               from capacity_planner.case_run
               where updated_at >= now()-interval '24 hours'
               group by 1 order by 1"""
        ).fetchall()
        error_types = conn.execute(
            """select split_part(last_error,':',1) error_type,count(*) occurrences,
                 max(updated_at) last_seen_at
               from capacity_planner.case_run
               where last_error is not null
               group by 1 order by occurrences desc,last_seen_at desc limit 10"""
        ).fetchall()
        memory_search = conn.execute(
            """select count(*) searches,
                 count(*) filter (where jsonb_array_length(
                   coalesce(payload->'items','[]'::jsonb)) > 0) searches_with_results,
                 coalesce(sum(jsonb_array_length(
                   coalesce(payload->'items','[]'::jsonb))),0) memories_returned,
                 count(*) filter (where jsonb_array_length(
                   coalesce(payload->'errors','[]'::jsonb)) > 0) degraded_searches,
                 max(created_at) last_search_at
               from capacity_planner.case_event where event_type='memory'"""
        ).fetchone()
        memory_search_statuses = conn.execute(
            """select coalesce(payload->>'status','UNKNOWN') status,count(*) search_count,
                 max(created_at) last_search_at
               from capacity_planner.case_event where event_type='memory'
               group by 1 order by search_count desc,status"""
        ).fetchall()
        memory_delivery = conn.execute(
            """select count(*) delivery_events,
                 count(*) filter (where status='COMPLETE') delivered,
                 count(*) filter (where status in ('QUEUED','RUNNING','RETRY')) pending,
                 count(*) filter (where status='FAILED') failed,
                 count(*) filter (where attempt_count > 1) retried,
                 coalesce(sum(attempt_count),0) delivery_attempts,
                 max(completed_at) last_delivered_at
               from capacity_planner.memory_outbox"""
        ).fetchone()
        memory_delivery_statuses = conn.execute(
            """select status,count(*) event_count,max(updated_at) last_updated_at
               from capacity_planner.memory_outbox
               group by status order by event_count desc,status"""
        ).fetchall()
        memory_delivery_errors = conn.execute(
            """select mo.company_id,c.company_name,c.ticker,mo.event_type,mo.status,
                 mo.attempt_count,mo.last_error,mo.updated_at
               from capacity_planner.memory_outbox mo
               join capacity_planner.company c using(company_id)
               where mo.last_error is not null
               order by mo.updated_at desc limit 20"""
        ).fetchall()
        jira_quality = conn.execute(
            """select count(*) total_requests,
                 count(*) filter (where status='COMPLETE') completed_requests,
                 count(*) filter (where status in ('QUEUED','RUNNING','RETRY'))
                   pending_requests,
                 count(*) filter (where status='FAILED') failed_requests,
                 count(*) filter (where status='COMPLETE' and
                   coalesce(jira_issue_key ~ '^[A-Z][A-Z0-9]*-[1-9][0-9]*$',false) and
                   coalesce(split_part(jira_issue_key,'-',1)=project_key,false) and
                   coalesce(jira_issue_url ~ '^https://',false) and
                   coalesce(right(jira_issue_url,length('/browse/'||jira_issue_key))=
                     '/browse/'||jira_issue_key,false))
                   completed_with_valid_link,
                 max(updated_at) last_checked_at
               from capacity_planner.jira_request"""
        ).fetchone()
        invalid_jira_links = conn.execute(
            """select jira_request_id,request_type,project_key,status,jira_issue_key,
                 jira_issue_url,last_error,updated_at
               from capacity_planner.jira_request
               where status='COMPLETE' and not (
                 coalesce(jira_issue_key ~ '^[A-Z][A-Z0-9]*-[1-9][0-9]*$',false) and
                 coalesce(split_part(jira_issue_key,'-',1)=project_key,false) and
                 coalesce(jira_issue_url ~ '^https://',false) and
                 coalesce(right(jira_issue_url,length('/browse/'||jira_issue_key))=
                   '/browse/'||jira_issue_key,false))
               order by updated_at desc"""
        ).fetchall()

    predicted = outcomes["predicted_positive"]
    precision = (
        round(100 * outcomes["true_positive"] / predicted, 2) if predicted else None
    )
    completed = orchestration["completed_cases"]
    total_terminal = completed + orchestration["failed_cases"]
    success_rate = round(100 * completed / total_terminal, 2) if total_terminal else None
    searches = memory_search["searches"]
    memory_hit_rate = (
        round(100 * memory_search["searches_with_results"] / searches, 2)
        if searches
        else None
    )
    delivery_events = memory_delivery["delivery_events"]
    memory_delivery_rate = (
        round(100 * memory_delivery["delivered"] / delivery_events, 2)
        if delivery_events
        else None
    )
    invalid_links = len(invalid_jira_links)
    ui_link_contract = evaluate_link_contract()
    ui_action_contract = evaluate_ui_action_contract()
    chatbot_contract = evaluate_portfolio_chat_contract()
    if jira_quality["total_requests"] == 0:
        jira_check_status = "NOT_EVALUATED"
    elif invalid_links or jira_quality["failed_requests"]:
        jira_check_status = "FAIL"
    elif jira_quality["pending_requests"]:
        jira_check_status = "PENDING"
    else:
        jira_check_status = "PASS"
    return {
        "data_quality": {
            **dict(quality),
            "checks": [dict(row) for row in quality_checks],
            "recent_runs": [dict(row) for row in recent_quality],
        },
        "evaluation": {
            **dict(eval_summary),
            **dict(outcomes),
            "precision_pct": precision,
            "precision_target_pct": 80,
            "precision_target_met": precision is not None and precision >= 80,
            "missing_specialists": [dict(row) for row in missing_specialists],
            "jira_handoff": {
                **dict(jira_quality),
                "invalid_completed_links": invalid_links,
                "mandatory_check_status": jira_check_status,
                "invalid_requests": [dict(row) for row in invalid_jira_links],
            },
            "ui_link_contract": ui_link_contract,
            "ui_action_contract": ui_action_contract,
            "chatbot_contract": chatbot_contract,
        },
        "orchestration": {
            **dict(orchestration),
            "terminal_success_rate_pct": success_rate,
            "node_counts": [dict(row) for row in node_counts],
            "recent_failures": [dict(row) for row in failures],
        },
        "observability": {
            **dict(observability),
            "stale_threshold_minutes": get_settings().stale_case_minutes,
            "hourly_throughput": [dict(row) for row in hourly_throughput],
            "error_types": [dict(row) for row in error_types],
        },
        "memory": {
            **dict(memory_search),
            **dict(memory_delivery),
            "search_hit_rate_pct": memory_hit_rate,
            "delivery_success_rate_pct": memory_delivery_rate,
            "search_statuses": [dict(row) for row in memory_search_statuses],
            "delivery_statuses": [dict(row) for row in memory_delivery_statuses],
            "recent_delivery_errors": [dict(row) for row in memory_delivery_errors],
        },
    }


def save_forecast_overrides(
    overrides: list[dict], *, modified_by: str, note: str
) -> int:
    with connection() as conn:
        for override in overrides:
            case_id = override["case_id"]
            case_row = conn.execute(
                """select company_id from capacity_planner.case_run
                   where case_id=%s and recommendation is not null""",
                (case_id,),
            ).fetchone()
            if not case_row:
                raise LookupError(f"Completed recommendation not found for case_id={case_id}")
            override_id = uuid.uuid4()
            conn.execute(
                """insert into capacity_planner.planner_forecast_override(
                   override_id,case_id,likelihood_pct,confidence,timing_days,
                   capacity_growth_tib,action,note,modified_by)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    override_id,
                    case_id,
                    override["likelihood_pct"],
                    override["confidence"],
                    override.get("timing_days"),
                    override.get("capacity_growth_tib"),
                    override["action"],
                    note,
                    modified_by,
                ),
            )
            conn.execute(
                """insert into capacity_planner.case_event(case_id,event_type,payload)
                   values (%s,'planner_forecast_override',%s)""",
                (
                    case_id,
                    json.dumps(
                        {
                            **override,
                            "override_id": str(override_id),
                            "modified_by": modified_by,
                            "note": note,
                        },
                        default=str,
                    ),
                ),
            )
    return len(overrides)


def capacity_availability(
    case_id: str, service: str, vault_type: str, requested_tib: float = 0
) -> list[dict]:
    tenancy_type = SERVICE_VAULT_TENANCY.get((service, vault_type))
    if not tenancy_type:
        raise LookupError(f"{vault_type} is not valid for {service}")
    with connection() as conn:
        case = conn.execute(
            """select c.company_id,c.region
               from capacity_planner.case_run cr
               join capacity_planner.company c using(company_id)
               where cr.case_id=%s""",
            (case_id,),
        ).fetchone()
        if not case:
            raise LookupError("Case not found")
        rows = conn.execute(
            """select i.*,
                 coalesce(h.planning_hold_tib,0) planning_hold_tib,
                 greatest(i.usable_capacity_tib-i.allocated_capacity_tib-
                   coalesce(h.planning_hold_tib,0),0) available_capacity_tib,
                 round(100*(i.allocated_capacity_tib+coalesce(h.planning_hold_tib,0)) /
                   i.usable_capacity_tib,2) current_allocation_pct,
                 (i.freshness_status='FRESH' and
                   i.source_updated_at >= now()-(%s * interval '1 hour')) inventory_usable
               from capacity_planner.capacity_inventory i
               left join lateral (
                 select sum(r.requested_tib) planning_hold_tib
                 from capacity_planner.local_capacity_reservation r
                 where r.status='LOCAL_RESERVED' and r.region=i.region and r.qfab=i.qfab
                   and r.service=i.service and r.vault_type=i.vault_type
               ) h on true
               where i.region=%s and i.service=%s and i.vault_type=%s
               order by available_capacity_tib desc,i.qfab""",
            (
                get_settings().capacity_inventory_max_age_hours,
                case["region"],
                service,
                vault_type,
            ),
        ).fetchall()
    request = Decimal(str(requested_tib))
    fresh_rows = [row for row in rows if row["inventory_usable"]]
    regional_available = sum(
        (Decimal(row["available_capacity_tib"]) for row in fresh_rows), Decimal(0)
    )
    regional_usable = sum(
        (Decimal(row["usable_capacity_tib"]) for row in fresh_rows), Decimal(0)
    )
    regional_allocated = sum(
        (Decimal(row["allocated_capacity_tib"]) + Decimal(row["planning_hold_tib"]) for row in fresh_rows),
        Decimal(0),
    )
    regional_sufficient = bool(fresh_rows) and request <= regional_available
    regional_post_pct = (
        ((regional_allocated + request) / regional_usable * 100).quantize(Decimal("0.01"))
        if regional_usable
        else Decimal(0)
    )
    result = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "company_id": case["company_id"],
                "customer_region": case["region"],
                "requested_tib": request,
                "regional_available_tib": regional_available,
                "available_after_tib": max(Decimal(0), regional_available - request),
                "shortfall_tib": max(Decimal(0), request - regional_available),
                "post_reservation_allocation_pct": regional_post_pct,
                "capacity_sufficient": regional_sufficient,
                "infrastructure_order_required": not regional_sufficient,
            }
        )
        result.append(item)
    return result


def capacity_inventory() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """select i.*,
                 coalesce(h.planning_hold_tib,0) planning_hold_tib,
                 greatest(i.usable_capacity_tib-i.allocated_capacity_tib-
                   coalesce(h.planning_hold_tib,0),0) available_capacity_tib,
                 round(100*(i.allocated_capacity_tib+coalesce(h.planning_hold_tib,0)) /
                   i.usable_capacity_tib,2) current_allocation_pct
               from capacity_planner.capacity_inventory i
               left join lateral (
                 select sum(r.requested_tib) planning_hold_tib
                 from capacity_planner.local_capacity_reservation r
                 where r.status='LOCAL_RESERVED' and r.region=i.region and r.qfab=i.qfab
                   and r.service=i.service and r.vault_type=i.vault_type
               ) h on true
               order by i.region,i.qfab,i.service,i.vault_type"""
        ).fetchall()
    return [dict(row) for row in rows]


def create_local_reservation(request: dict) -> dict:
    tenancy_type = SERVICE_VAULT_TENANCY.get(
        (request["service"], request["vault_type"])
    )
    if not tenancy_type:
        raise LookupError(
            f"{request['vault_type']} is not valid for {request['service']}"
        )
    case_id = request["case_id"]
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (str(case_id),))
        case = conn.execute(
            """select cr.company_id,cr.status,cr.recommendation,c.region
               from capacity_planner.case_run cr
               join capacity_planner.company c using(company_id)
               where cr.case_id=%s and cr.recommendation is not null""",
            (case_id,),
        ).fetchone()
        if not case or case["status"] not in ("REVIEW_REQUIRED", "COMPLETE"):
            raise LookupError(f"Completed recommendation not found for case_id={case_id}")
        existing = conn.execute(
            """select * from capacity_planner.local_capacity_reservation
               where case_id=%s""",
            (case_id,),
        ).fetchone()
        if existing:
            return {**dict(existing), "created": False}

        if request["region"] != case["region"]:
            raise LookupError(
                f"Reservation region must match customer region {case['region']}"
            )
        inventories = conn.execute(
            """select i.*,
                 (freshness_status='FRESH' and
                   source_updated_at >= now()-(%s * interval '1 hour')) inventory_usable,
                 coalesce((
                   select sum(r.requested_tib) from capacity_planner.local_capacity_reservation r
                   where r.status='LOCAL_RESERVED' and r.region=i.region and r.qfab=i.qfab
                     and r.service=i.service and r.vault_type=i.vault_type
                 ),0) planning_hold_tib
               from capacity_planner.capacity_inventory i
               where region=%s and service=%s and vault_type=%s
               for update""",
            (
                get_settings().capacity_inventory_max_age_hours,
                case["region"],
                request["service"],
                request["vault_type"],
            ),
        ).fetchall()
        inventory = next((row for row in inventories if row["qfab"] == request["qfab"]), None)
        if not inventory or not inventory["inventory_usable"]:
            raise CapacityUnavailableError(
                {
                    "reason": "INVENTORY_UNAVAILABLE",
                    "message": "Capacity inventory is missing or stale for the selected pool",
                    "region": case["region"],
                    "qfab": request["qfab"],
                }
            )
        requested = Decimal(str(request["requested_tib"]))
        fresh_inventories = [row for row in inventories if row["inventory_usable"]]
        available_before = sum(
            (
                Decimal(row["usable_capacity_tib"])
                - Decimal(row["allocated_capacity_tib"])
                - Decimal(row["planning_hold_tib"])
                for row in fresh_inventories
            ),
            Decimal(0),
        )
        if requested > available_before:
            raise CapacityUnavailableError(
                {
                    "reason": "INSUFFICIENT_CAPACITY",
                    "message": "Insufficient capacity; new infrastructure is required",
                    "region": case["region"],
                    "requested_tib": float(requested),
                    "available_tib": float(max(Decimal(0), available_before)),
                    "shortfall_tib": float(requested - available_before),
                }
            )
        available_after = available_before - requested
        post_pct = (
            (
                sum(
                    (
                        Decimal(row["allocated_capacity_tib"])
                        + Decimal(row["planning_hold_tib"])
                        for row in fresh_inventories
                    ),
                    Decimal(0),
                )
                + requested
            )
            / sum((Decimal(row["usable_capacity_tib"]) for row in fresh_inventories), Decimal(0))
            * 100
        ).quantize(Decimal("0.01"))
        infrastructure_order_recommended = False

        reservation_id = uuid.uuid4()
        row = conn.execute(
            """insert into capacity_planner.local_capacity_reservation(
               reservation_id,case_id,company_id,requested_tib,target_date,service,vault_type,
               tenancy_type,region,qfab,planner_identity,note,inventory_id,
               available_before_tib,available_after_tib,post_reservation_allocation_pct,
               infrastructure_order_recommended)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               returning *""",
            (
                reservation_id,
                case_id,
                case["company_id"],
                request["requested_tib"],
                request["target_date"],
                request["service"],
                request["vault_type"],
                tenancy_type,
                case["region"],
                request["qfab"],
                request["planner_identity"],
                request.get("note", ""),
                inventory["inventory_id"],
                available_before,
                available_after,
                post_pct,
                infrastructure_order_recommended,
            ),
        ).fetchone()
        conn.execute(
            """insert into capacity_planner.planner_decision(case_id,decision,note,decided_by)
               values (%s,'APPROVE_REVIEW',%s,%s)""",
            (case_id, request.get("note", ""), request["planner_identity"]),
        )
        if get_settings().mem0_enabled:
            enqueue_planner_decision(
                conn,
                company_id=case["company_id"],
                case_id=str(case_id),
                decision="APPROVE_REVIEW",
                recommendation=case["recommendation"],
            )
        conn.execute(
            """insert into capacity_planner.case_event(case_id,event_type,payload)
               values (%s,'local_capacity_reservation',%s)""",
            (
                case_id,
                json.dumps(
                    {
                        "reservation_id": str(reservation_id),
                        "requested_tib": request["requested_tib"],
                        "target_date": request["target_date"],
                        "service": request["service"],
                        "vault_type": request["vault_type"],
                        "tenancy_type": tenancy_type,
                        "region": case["region"],
                        "qfab": request["qfab"],
                        "available_before_tib": available_before,
                        "available_after_tib": available_after,
                        "post_reservation_allocation_pct": post_pct,
                        "infrastructure_order_recommended": (
                            infrastructure_order_recommended
                        ),
                        "status": "LOCAL_RESERVED",
                        "source": "LOCAL_PLANNING_ONLY",
                        "planner_identity": request["planner_identity"],
                    },
                    default=str,
                ),
            ),
        )
    return {**dict(row), "created": True}


def local_reservations(case_id: str | None = None) -> list[dict]:
    sql = """select r.*,c.company_name,c.ticker
             from capacity_planner.local_capacity_reservation r
             join capacity_planner.company c using(company_id)"""
    params = ()
    if case_id:
        sql += " where r.case_id=%s"
        params = (case_id,)
    sql += " order by r.created_at desc"
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def recover_stale_cases() -> int:
    with connection() as conn:
        cursor = conn.execute(
            """update capacity_planner.case_run
               set status='RETRY',locked_at=null,locked_by=null,
                   last_error='Worker lease expired; case returned to retry queue',updated_at=now()
               where status='RUNNING'
                 and locked_at < now()-(%s * interval '1 minute')""",
            (get_settings().stale_case_minutes,),
        )
    return cursor.rowcount


def claim_case(worker_id: str) -> dict | None:
    settings = get_settings()
    with connection() as conn:
        row = conn.execute(
            """
            select * from capacity_planner.case_run
            where status in ('QUEUED','RETRY') and attempt_count < %s
            order by priority,created_at for update skip locked limit 1
            """,
            (settings.max_agent_attempts,),
        ).fetchone()
        if not row:
            return None
        row = conn.execute(
            """update capacity_planner.case_run set status='RUNNING',attempt_count=attempt_count+1,
               locked_at=now(),locked_by=%s,updated_at=now() where case_id=%s returning *""",
            (worker_id, row["case_id"]),
        ).fetchone()
    return dict(row)


def finish_case(case_id: str, recommendation: dict) -> None:
    status = "REVIEW_REQUIRED" if recommendation.get("action") == "PLANNER_REVIEW" else "COMPLETE"
    with connection() as conn:
        conn.execute(
            "update capacity_planner.case_run set status=%s,recommendation=%s,updated_at=now(),locked_at=null,locked_by=null where case_id=%s",
            (status, json.dumps(recommendation), case_id),
        )


def fail_case(case: dict, error: Exception) -> None:
    terminal = case["attempt_count"] >= get_settings().max_agent_attempts
    with connection() as conn:
        conn.execute(
            "update capacity_planner.case_run set status=%s,last_error=%s,updated_at=now(),locked_at=null,locked_by=null where case_id=%s",
            ("FAILED" if terminal else "RETRY", f"{type(error).__name__}: {error}"[:2000], case["case_id"]),
        )
