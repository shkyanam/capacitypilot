import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

from .config import get_settings
from .db import connection, migrate
from .jira_outbox import enqueue_jira_request, jira_requests
from .memory import list_application_memories
from .memory_outbox import enqueue_planner_decision
from .models import (
    BulkForecastOverrideRequest,
    DecisionRequest,
    InvestigationRequest,
    JiraRequestCreate,
    LocalReservationRequest,
    PortfolioChatRequest,
    SlackDigestRequest,
)
from .news_jobs import enqueue_all, latest_comparison, status_counts
from .portfolio_chat import answer_portfolio_question
from .repository import (
    CapacityUnavailableError,
    capacity_availability,
    capacity_inventory,
    create_case,
    create_local_reservation,
    enqueue_initial_portfolio,
    local_reservations,
    portfolio_status,
    quality_eval_status,
    save_forecast_overrides,
)
from .slack_outbox import capacity_alert_summary, enqueue_capacity_digest, slack_alerts

LOG = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate()
    queued = enqueue_initial_portfolio()
    if queued:
        LOG.info("initial_portfolio_queued count=%s", queued)
    yield


app = FastAPI(title="CapacityPilot API", version="1.0.0", lifespan=lifespan)


def authorize(x_api_key: str = Header(default="")) -> None:
    if x_api_key != get_settings().api_auth_token:
        raise HTTPException(401, "Invalid API credential")


@app.get("/health")
def health():
    with connection() as conn:
        conn.execute("select 1")
    return {"status": "ok"}


@app.get("/companies", dependencies=[Depends(authorize)])
def companies(limit: int = 1000):
    limit = min(max(limit, 1), 1000)
    with connection() as conn:
        rows = conn.execute(
            """select c.*,s.installed_tib,s.consumed_tib,
               round(s.consumed_tib/s.installed_tib*100,2) utilization_pct,
               s.trailing_12m_growth_tib,s.open_demand_tib,s.demand_stage,
               s.news_event_count,s.news_relevance,s.source_freshness,s.generated_at,
               latest_case.case_id latest_case_id,latest_case.status latest_case_status,
               latest_case.updated_at latest_case_updated_at,
               recommendation.case_id recommendation_case_id,
               (recommendation.recommendation->>'likelihood_pct')::numeric likelihood_pct,
               recommendation.recommendation->>'confidence' confidence,
               (recommendation.recommendation->>'timing_days')::integer timing_days,
               (recommendation.recommendation->>'capacity_growth_tib')::numeric
                 suggested_growth_tib,
               recommendation.recommendation->>'action' recommended_action,
               recommendation.updated_at last_recommendation_at,
               decision.decision planner_decision,decision.decided_by,
               decision.decided_at,
               coalesce(reservation.reserved_tib,0) reserved_tib
               from capacity_planner.company c
               join capacity_planner.capacity_signal s using(company_id)
               left join lateral (
                 select cr.case_id,cr.status,cr.updated_at
                 from capacity_planner.case_run cr
                 where cr.company_id=c.company_id
                 order by cr.created_at desc limit 1
               ) latest_case on true
               left join lateral (
                 select cr.case_id,cr.recommendation,cr.updated_at
                 from capacity_planner.case_run cr
                 where cr.company_id=c.company_id and cr.recommendation is not null
                 order by cr.created_at desc limit 1
               ) recommendation on true
               left join lateral (
                 select pd.decision,pd.decided_by,pd.decided_at
                 from capacity_planner.planner_decision pd
                 join capacity_planner.case_run cr using(case_id)
                 where cr.company_id=c.company_id
                 order by pd.decided_at desc limit 1
               ) decision on true
               left join lateral (
                 select sum(lr.requested_tib) reserved_tib
                 from capacity_planner.local_capacity_reservation lr
                 where lr.company_id=c.company_id and lr.status='LOCAL_RESERVED'
               ) reservation on true
               order by utilization_pct desc limit %s""",
            (limit,),
        ).fetchall()
    return rows


@app.post("/portfolio/chat", dependencies=[Depends(authorize)])
def portfolio_chat(request: PortfolioChatRequest):
    return answer_portfolio_question(
        request.question,
        context=request.context.model_dump(mode="json") if request.context else None,
    )


@app.get("/shortlist", dependencies=[Depends(authorize)])
def shortlist(
    min_likelihood: float = 75,
    pending_only: bool = False,
    alert_eligible_only: bool = False,
    exclude_simulations: bool = False,
):
    threshold = min(max(min_likelihood, 0), 100)
    pending_filter = """
               and not exists (
                 select 1 from capacity_planner.planner_decision pd
                 where pd.case_id=r.case_id
               )
               and not exists (
                 select 1 from capacity_planner.local_capacity_reservation lr
                 where lr.case_id=r.case_id and lr.status='LOCAL_RESERVED'
               )""" if pending_only else ""
    alert_filter = """
               and coalesce((r.recommendation->>'alert_allowed')::boolean,false)
               and coalesce(o.confidence,r.recommendation->>'confidence')
                 in ('MEDIUM','HIGH')
               and coalesce(o.action,r.recommendation->>'action')='PLANNER_REVIEW'
               and coalesce(o.capacity_growth_tib,
                 (r.recommendation->>'capacity_growth_tib')::numeric) > 0""" if alert_eligible_only else ""
    simulation_filter = "and r.scenario_id is null" if exclude_simulations else ""
    with connection() as conn:
        rows = conn.execute(
            f"""select c.company_id,c.company_name,c.ticker,c.exchange,c.region,r.case_id,r.status,
               (r.scenario_id is not null) test_scenario,
               coalesce(o.likelihood_pct,(r.recommendation->>'likelihood_pct')::numeric)
                 likelihood_pct,
               coalesce(o.confidence,r.recommendation->>'confidence') confidence,
               coalesce(o.action,r.recommendation->>'action') action,
               coalesce(o.timing_days,(r.recommendation->>'timing_days')::integer) timing_days,
               coalesce(o.capacity_growth_tib,
                 (r.recommendation->>'capacity_growth_tib')::numeric) capacity_growth_tib,
               (r.recommendation->>'likelihood_pct')::numeric ai_likelihood_pct,
               r.recommendation->>'confidence' ai_confidence,
               (r.recommendation->>'timing_days')::integer ai_timing_days,
               (r.recommendation->>'capacity_growth_tib')::numeric ai_capacity_growth_tib,
               r.recommendation->>'action' ai_action,
               o.override_id is not null planner_adjusted,
               o.modified_by planner_modified_by,o.created_at planner_modified_at,
               o.note planner_note,
               coalesce(sc.installed_tib,s.installed_tib) installed_tib,
               coalesce(sc.consumed_tib,s.consumed_tib) consumed_tib,
               round(coalesce(sc.consumed_tib,s.consumed_tib) /
                 coalesce(sc.installed_tib,s.installed_tib)*100,2) utilization_pct,
               coalesce(sc.trailing_12m_growth_tib,s.trailing_12m_growth_tib)
                 trailing_12m_growth_tib,
               coalesce(sc.open_demand_tib,s.open_demand_tib) open_demand_tib,
               coalesce(sc.demand_stage,s.demand_stage) demand_stage,
               (dq.payload->>'quality_score_pct')::numeric quality_score_pct,
               coalesce((dq.payload->>'production_eligible')::boolean,false) production_eligible,
               r.updated_at
               from capacity_planner.company c
               join capacity_planner.capacity_signal s using(company_id)
               join lateral (
                 select * from capacity_planner.case_run cr
                 where cr.company_id=c.company_id and cr.recommendation is not null
                 order by cr.created_at desc limit 1
               ) r on true
               left join capacity_planner.capacity_signal_scenario sc
                 on sc.scenario_id=r.scenario_id
               left join lateral (
                 select * from capacity_planner.planner_forecast_override pfo
                 where pfo.case_id=r.case_id order by pfo.created_at desc limit 1
               ) o on true
               left join lateral (
                 select payload from capacity_planner.case_event ce
                 where ce.case_id=r.case_id and ce.event_type='data_quality'
                 order by ce.event_id desc limit 1
               ) dq on true
               where coalesce(o.likelihood_pct,
                 (r.recommendation->>'likelihood_pct')::numeric) >= %s
               {pending_filter}
               {alert_filter}
               {simulation_filter}
               order by likelihood_pct desc,capacity_growth_tib desc nulls last""",
            (threshold,),
        ).fetchall()
    return rows


@app.post("/shortlist/overrides", status_code=201, dependencies=[Depends(authorize)])
def bulk_forecast_override(request: BulkForecastOverrideRequest):
    case_ids = [item.case_id for item in request.overrides]
    if len(case_ids) != len(set(case_ids)):
        raise HTTPException(422, "Each case may appear only once in a bulk update")
    try:
        updated = save_forecast_overrides(
            [item.model_dump(mode="json") for item in request.overrides],
            modified_by=request.modified_by,
            note=request.note,
        )
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"updated": updated}


@app.post("/reservations", status_code=201, dependencies=[Depends(authorize)])
def reserve_locally(request: LocalReservationRequest):
    try:
        result = create_local_reservation(request.model_dump(mode="json"))
    except CapacityUnavailableError as exc:
        raise HTTPException(409, exc.details) from exc
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    jira_handoffs = []
    jira_errors = []
    if get_settings().jira_enabled and result.get("case_id"):
        request_types = ["CAP_RESERVATION"]
        if result.get("infrastructure_order_recommended"):
            request_types.append("HUB_INFRASTRUCTURE")
        for request_type in request_types:
            try:
                jira_handoffs.append(
                    enqueue_jira_request(
                        {
                            "case_id": str(request.case_id),
                            "request_type": request_type,
                            "planner_identity": request.planner_identity,
                            "note": request.note,
                        }
                    )
                )
            except (LookupError, RuntimeError) as exc:
                LOG.exception(
                    "reservation_jira_enqueue_failed case_id=%s request_type=%s",
                    request.case_id,
                    request_type,
                )
                jira_errors.append({"request_type": request_type, "error": str(exc)})
    return {**result, "jira_handoffs": jira_handoffs, "jira_errors": jira_errors}


@app.get("/reservations", dependencies=[Depends(authorize)])
def reservations(case_id: str | None = None):
    return local_reservations(case_id)


@app.get("/capacity-inventory", dependencies=[Depends(authorize)])
def inventory():
    return capacity_inventory()


@app.get("/capacity-availability", dependencies=[Depends(authorize)])
def availability(
    case_id: str,
    service: str,
    vault_type: str,
    requested_tib: float = 0,
):
    try:
        return capacity_availability(case_id, service, vault_type, requested_tib)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/jira-requests", status_code=202, dependencies=[Depends(authorize)])
def create_jira_request(request: JiraRequestCreate):
    try:
        return enqueue_jira_request(request.model_dump(mode="json"))
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/jira-requests", dependencies=[Depends(authorize)])
def list_jira_requests(case_id: str | None = None):
    return jira_requests(case_id)


@app.get("/slack-alerts/summary", dependencies=[Depends(authorize)])
def slack_summary():
    return capacity_alert_summary()


@app.get("/slack-alerts", dependencies=[Depends(authorize)])
def list_slack_alerts(limit: int = 100):
    return slack_alerts(min(max(limit, 1), 1000))


@app.post("/slack-alerts/enqueue", status_code=202, dependencies=[Depends(authorize)])
def enqueue_slack_alert(request: SlackDigestRequest):
    try:
        row = enqueue_capacity_digest(force=request.force)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"queued": bool(row and row.get("created")), "alert": row}


@app.post(
    "/portfolio-investigation/enqueue",
    status_code=202,
    dependencies=[Depends(authorize)],
)
def enqueue_portfolio_investigation():
    queued = enqueue_initial_portfolio()
    return {"queued": queued, **portfolio_status()}


@app.get("/portfolio-investigation/status", dependencies=[Depends(authorize)])
def portfolio_investigation_status():
    return portfolio_status()


@app.post("/news-ingestion/enqueue", status_code=202, dependencies=[Depends(authorize)])
def enqueue_news_ingestion():
    return {"queued_or_refreshed": enqueue_all(), **status_counts()}


@app.get("/news-ingestion/status", dependencies=[Depends(authorize)])
def news_ingestion_status():
    return status_counts()


@app.get("/news-ingestion/comparisons/latest", dependencies=[Depends(authorize)])
def latest_news_comparison():
    return latest_comparison() or {"rows": []}


@app.post("/cases", status_code=202, dependencies=[Depends(authorize)])
def investigate(request: InvestigationRequest):
    try:
        return create_case(request.company_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/cases/{case_id}", dependencies=[Depends(authorize)])
def case(case_id: str):
    with connection() as conn:
        row = conn.execute("select * from capacity_planner.case_run where case_id=%s", (case_id,)).fetchone()
        events = conn.execute(
            "select event_type,payload,created_at from capacity_planner.case_event where case_id=%s order by event_id",
            (case_id,),
        ).fetchall()
    if not row:
        raise HTTPException(404, "Case not found")
    return {**row, "events": events}


@app.post("/cases/{case_id}/decisions", status_code=201, dependencies=[Depends(authorize)])
def decide(case_id: str, request: DecisionRequest):
    with connection() as conn:
        case_row = conn.execute(
            "select status,company_id,recommendation from capacity_planner.case_run where case_id=%s",
            (case_id,),
        ).fetchone()
        if not case_row:
            raise HTTPException(404, "Case not found")
        if case_row["status"] not in ("REVIEW_REQUIRED", "COMPLETE"):
            raise HTTPException(409, "Case is not ready for a planner decision")
        row = conn.execute(
            """insert into capacity_planner.planner_decision(case_id,decision,note,decided_by)
               values (%s,%s,%s,%s) returning *""",
            (case_id, request.decision, request.note, request.decided_by),
        ).fetchone()
        if get_settings().mem0_enabled:
            enqueue_planner_decision(
                conn,
                company_id=case_row["company_id"],
                case_id=case_id,
                decision=request.decision,
                recommendation=case_row["recommendation"] or {},
                planner_comment=request.note,
            )
    return row


@app.get("/evaluation", dependencies=[Depends(authorize)])
def evaluation():
    with connection() as conn:
        row = conn.execute(
            """select count(*) labeled,
               count(*) filter (where o.expanded and (r.recommendation->>'likelihood_pct')::numeric >= 80) true_positive,
               count(*) filter (where (r.recommendation->>'likelihood_pct')::numeric >= 80) predicted_positive
               from capacity_planner.prediction_outcome o
               join capacity_planner.case_run r using(case_id)"""
        ).fetchone()
    predicted = row["predicted_positive"]
    precision = round(100 * row["true_positive"] / predicted, 2) if predicted else None
    return {
        **row,
        "precision_pct": precision,
        "target_pct": 80,
        "target_met": precision is not None and precision >= 80,
    }


@app.get("/quality-evals", dependencies=[Depends(authorize)])
def quality_evals():
    return quality_eval_status()


@app.get("/memories", dependencies=[Depends(authorize)])
def memories():
    result = list_application_memories()
    company_ids = sorted(
        {
            int(item.get("metadata", {}).get("company_id"))
            for item in result.get("items", [])
            if str(item.get("metadata", {}).get("company_id", "")).isdigit()
        }
    )
    companies_by_id = {}
    if company_ids:
        with connection() as conn:
            rows = conn.execute(
                """select company_id,company_name,ticker
                   from capacity_planner.company where company_id=any(%s)""",
                (company_ids,),
            ).fetchall()
        companies_by_id = {row["company_id"]: row for row in rows}

    enriched = []
    for item in result.get("items", []):
        company_id = item.get("metadata", {}).get("company_id")
        try:
            company = companies_by_id.get(int(company_id), {})
        except (TypeError, ValueError):
            company = {}
        enriched.append(
            {
                **item,
                "company_name": company.get("company_name"),
                "ticker": company.get("ticker"),
            }
        )
    return {**result, "items": enriched, "count": len(enriched)}


def main() -> None:
    uvicorn.run("capacity_planner.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
