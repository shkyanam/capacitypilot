import hashlib
import json
from typing import Any
from uuid import uuid4

from .config import get_settings
from .db import connection


def capacity_alert_summary() -> dict[str, Any]:
    settings = get_settings()
    with connection() as conn:
        rows = conn.execute(
            """with latest_case as (
                 select distinct on (cr.company_id)
                   cr.case_id,cr.company_id,cr.scenario_id,cr.recommendation,cr.updated_at
                 from capacity_planner.case_run cr
                 where cr.recommendation is not null
                 order by cr.company_id,cr.created_at desc
               ), effective as (
                 select lc.*,c.company_name,c.ticker,c.region,
                   coalesce(o.action,lc.recommendation->>'action') action,
                   coalesce(o.capacity_growth_tib,
                     (lc.recommendation->>'capacity_growth_tib')::numeric) growth_tib,
                   coalesce((dq.payload->>'production_eligible')::boolean,false)
                     production_eligible
                 from latest_case lc
                 join capacity_planner.company c using(company_id)
                 left join lateral (
                   select * from capacity_planner.planner_forecast_override p
                   where p.case_id=lc.case_id order by p.created_at desc limit 1
                 ) o on true
                 left join lateral (
                   select payload from capacity_planner.case_event ce
                   where ce.case_id=lc.case_id and ce.event_type='data_quality'
                   order by ce.event_id desc limit 1
                 ) dq on true
                 where coalesce((lc.recommendation->>'alert_allowed')::boolean,false)
                   and coalesce(o.confidence,lc.recommendation->>'confidence')
                     in ('MEDIUM','HIGH')
                   and coalesce(o.likelihood_pct,
                     (lc.recommendation->>'likelihood_pct')::numeric) >= 80
                   and (%s or lc.scenario_id is null)
                   and not exists (
                     select 1 from capacity_planner.planner_decision pd
                     where pd.case_id=lc.case_id
                   )
                   and not exists (
                     select 1 from capacity_planner.local_capacity_reservation lr
                     where lr.case_id=lc.case_id and lr.status='LOCAL_RESERVED'
                   )
               )
               select e.*,coalesce(cap.regional_available_tib,0) regional_available_tib
               from effective e
               left join lateral (
                   select
                     sum(greatest(i.usable_capacity_tib-i.allocated_capacity_tib-
                       coalesce(h.planning_hold_tib,0),0)) regional_available_tib
                   from capacity_planner.capacity_inventory i
                   left join lateral (
                     select sum(lr.requested_tib) planning_hold_tib
                     from capacity_planner.local_capacity_reservation lr
                     where lr.status='LOCAL_RESERVED' and lr.inventory_id=i.inventory_id
                   ) h on true
                   where i.region=e.region and i.freshness_status='FRESH'
                     and i.source_updated_at >= now()-(%s * interval '1 hour')
               ) cap on true
               where e.action='PLANNER_REVIEW' and e.growth_tib > 0
                 and (not %s or e.production_eligible)
               order by e.growth_tib desc,e.company_name""",
            (
                settings.slack_include_test_scenarios,
                settings.capacity_inventory_max_age_hours,
                settings.slack_require_production_eligible,
            ),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        route = (
            "RESERVE_CAPACITY"
            if item["regional_available_tib"] >= item["growth_tib"]
            else "ORDER_MORE_STORAGE"
        )
        items.append(
            {
                "case_id": str(item["case_id"]),
                "company_id": item["company_id"],
                "company_name": item["company_name"],
                "ticker": item["ticker"],
                "region": item["region"],
                "growth_tib": item["growth_tib"],
                "regional_available_tib": item["regional_available_tib"],
                "route": route,
                "test_scenario": item["scenario_id"] is not None,
                "updated_at": item["updated_at"],
            }
        )
    reserve = [item for item in items if item["route"] == "RESERVE_CAPACITY"]
    order = [item for item in items if item["route"] == "ORDER_MORE_STORAGE"]
    scenario_count = sum(1 for item in items if item["test_scenario"])
    return {
        "demand_review_count": len(items),
        "reserve_capacity_count": len(reserve),
        "order_more_storage_count": len(order),
        "reserve_capacity": reserve[:10],
        "order_more_storage": order[:10],
        "test_scenario_count": scenario_count,
        "test_mode": scenario_count > 0,
        "production_filter_enabled": settings.slack_require_production_eligible,
        "planner_url": settings.slack_planner_url,
    }


def enqueue_capacity_digest(*, force: bool = False) -> dict[str, Any] | None:
    settings = get_settings()
    if not settings.slack_enabled:
        raise RuntimeError("Slack integration is disabled")
    payload = capacity_alert_summary()
    if payload["demand_review_count"] == 0:
        return None
    fingerprint = json.dumps(payload, sort_keys=True, default=str)
    dedupe_key = hashlib.sha256(fingerprint.encode()).hexdigest()
    with connection() as conn:
        if not force:
            recent = conn.execute(
                """select 1 from capacity_planner.slack_alert_outbox
                   where created_at >= now()-(%s * interval '1 minute') limit 1""",
                (settings.slack_digest_interval_minutes,),
            ).fetchone()
            if recent:
                return None
        row = conn.execute(
            """insert into capacity_planner.slack_alert_outbox(
               alert_id,dedupe_key,payload) values (%s,%s,%s)
               on conflict(dedupe_key) do update set updated_at=now()
               returning *, (xmax = 0) created""",
            (uuid4(), dedupe_key, json.dumps(payload, default=str)),
        ).fetchone()
    result = dict(row)
    return {**result, "created": bool(result.get("created", True))}


def slack_alerts(limit: int = 100) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """select * from capacity_planner.slack_alert_outbox
               order by created_at desc limit %s""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_slack_alert(worker_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """select * from capacity_planner.slack_alert_outbox
               where status in ('QUEUED','RETRY') and available_at <= now()
               order by created_at for update skip locked limit 1"""
        ).fetchone()
        if not row:
            return None
        row = conn.execute(
            """update capacity_planner.slack_alert_outbox set status='RUNNING',
               attempt_count=attempt_count+1,locked_at=now(),locked_by=%s,updated_at=now()
               where alert_id=%s returning *""",
            (worker_id, row["alert_id"]),
        ).fetchone()
    return dict(row)


def complete_slack_alert(alert_id: str, result: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """update capacity_planner.slack_alert_outbox set status='COMPLETE',
               slack_channel=%s,slack_message_ts=%s,completed_at=now(),last_error=null,
               locked_at=null,locked_by=null,updated_at=now() where alert_id=%s""",
            (result.get("channel"), result.get("message_ts"), alert_id),
        )


def fail_slack_alert(item: dict[str, Any], error: Exception) -> None:
    maximum = get_settings().slack_max_attempts
    status = "FAILED" if item["attempt_count"] >= maximum else "RETRY"
    delay_minutes = min(60, 2 ** item["attempt_count"])
    with connection() as conn:
        conn.execute(
            """update capacity_planner.slack_alert_outbox set status=%s,last_error=%s,
               available_at=now()+(%s * interval '1 minute'),locked_at=null,locked_by=null,
               updated_at=now() where alert_id=%s""",
            (
                status,
                f"{type(error).__name__}: {error}"[:2000],
                delay_minutes,
                item["alert_id"],
            ),
        )
