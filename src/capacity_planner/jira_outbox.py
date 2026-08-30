import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .config import get_settings
from .db import connection
from .repository import capacity_availability


def _recommended_order_tib(option: dict) -> Decimal:
    usable = Decimal(str(option["usable_capacity_tib"]))
    allocated_after = (
        Decimal(str(option["allocated_capacity_tib"]))
        + Decimal(str(option["planning_hold_tib"]))
        + Decimal(str(option["requested_tib"]))
    )
    restore_threshold = max(Decimal(0), allocated_after / Decimal("0.70") - usable)
    return max(Decimal(str(option["shortfall_tib"])), restore_threshold).quantize(
        Decimal("0.01")
    )


def _enqueue_regional_hub_request(request: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    tenancy_type = {
        "Standard": "Shared",
        "High Performance": "Shared",
        "Ultra Performance": "Shared",
        "System Standard": "Dedicated",
        "System Critical": "Dedicated",
        "Replication": "Replicated",
        "General Purpose": "Replicated",
    }.get(request["vault_type"])
    if not tenancy_type:
        raise LookupError("Unsupported storage tier")

    pool_key = "|".join(
        (
            request["region"],
            request["qfab"],
            request["service"],
            request["vault_type"],
        )
    )
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (pool_key,))
        inventory = conn.execute(
            """select i.*,
                 coalesce(h.planning_hold_tib,0) planning_hold_tib,
                 greatest(i.usable_capacity_tib-i.allocated_capacity_tib-
                   coalesce(h.planning_hold_tib,0),0) available_capacity_tib,
                 (i.freshness_status='FRESH' and
                   i.source_updated_at >= now()-(%s * interval '1 hour')) inventory_usable
               from capacity_planner.capacity_inventory i
               left join lateral (
                 select sum(r.requested_tib) planning_hold_tib
                 from capacity_planner.local_capacity_reservation r
                 where r.status='LOCAL_RESERVED' and r.region=i.region and r.qfab=i.qfab
                   and r.service=i.service and r.vault_type=i.vault_type
               ) h on true
               where i.region=%s and i.qfab=%s and i.service=%s and i.vault_type=%s""",
            (
                settings.capacity_inventory_max_age_hours,
                request["region"],
                request["qfab"],
                request["service"],
                request["vault_type"],
            ),
        ).fetchone()
        if not inventory:
            raise LookupError("Regional capacity pool not found")
        if not inventory["inventory_usable"]:
            raise LookupError("Fresh regional capacity data is required for a HUB request")
        existing = conn.execute(
            """select * from capacity_planner.jira_request
               where case_id is null and request_type='HUB_INFRASTRUCTURE'
                 and region=%s and qfab=%s and service=%s and vault_type=%s
                 and status in ('QUEUED','RUNNING','RETRY')
               order by created_at desc limit 1""",
            (
                request["region"],
                request["qfab"],
                request["service"],
                request["vault_type"],
            ),
        ).fetchone()
        if existing:
            return {**dict(existing), "created": False}

        requested_tib = Decimal(str(request["requested_tib"])).quantize(Decimal("0.01"))
        details = {
            "request_scope": "regional_capacity",
            "region": request["region"],
            "qfab": request["qfab"],
            "service": request["service"],
            "vault_type": request["vault_type"],
            "tenancy_type": tenancy_type,
            "requested_tib": requested_tib,
            "available_tib": inventory["available_capacity_tib"],
            "usable_capacity_tib": inventory["usable_capacity_tib"],
            "allocated_capacity_tib": inventory["allocated_capacity_tib"],
            "planning_hold_tib": inventory["planning_hold_tib"],
            "required_by": request["target_date"],
            "inventory_id": inventory["inventory_id"],
            "planner_identity": request["planner_identity"],
            "planner_note": request.get("note", ""),
        }
        description_lines = [
            "Request type: HUB_INFRASTRUCTURE",
            *[
                f"{key.replace('_', ' ').title()}: {value}"
                for key, value in details.items()
            ],
        ]
        jira_request_id = uuid4()
        summary = (
            f"Order {float(requested_tib):,.1f} TiB infrastructure for "
            f"{request['region']}/{request['qfab']}"
        )
        payload = {**details, "description_lines": description_lines}
        row = conn.execute(
            """insert into capacity_planner.jira_request(
               jira_request_id,case_id,reservation_id,company_id,inventory_id,request_type,
               project_key,issue_type,summary,payload,planner_identity,region,qfab,service,
               vault_type,tenancy_type,requested_tib,target_date)
               values (%s,null,null,null,%s,'HUB_INFRASTRUCTURE',%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s)
               returning *, true created""",
            (
                jira_request_id,
                inventory["inventory_id"],
                settings.jira_hub_project_key,
                settings.jira_hub_issue_type,
                summary,
                json.dumps(payload, default=str),
                request["planner_identity"],
                request["region"],
                request["qfab"],
                request["service"],
                request["vault_type"],
                tenancy_type,
                requested_tib,
                request["target_date"],
            ),
        ).fetchone()
    return {**dict(row), "created": True}


def enqueue_jira_request(request: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.jira_enabled:
        raise RuntimeError("Jira integration is disabled")
    if request["request_type"] == "HUB_INFRASTRUCTURE" and not request.get("case_id"):
        return _enqueue_regional_hub_request(request)

    case_id = str(request["case_id"])
    request_type = request["request_type"]

    with connection() as conn:
        case = conn.execute(
            """select cr.company_id,cr.recommendation,c.company_name,c.ticker,c.region
               from capacity_planner.case_run cr
               join capacity_planner.company c using(company_id)
               where cr.case_id=%s and cr.recommendation is not null""",
            (case_id,),
        ).fetchone()
        if not case:
            raise LookupError("Completed recommendation not found")
        existing = conn.execute(
            """select * from capacity_planner.jira_request
               where case_id=%s and request_type=%s""",
            (case_id, request_type),
        ).fetchone()
        if existing:
            return {**dict(existing), "created": False}
        reservation = conn.execute(
            """select * from capacity_planner.local_capacity_reservation
               where case_id=%s and status='LOCAL_RESERVED'""",
            (case_id,),
        ).fetchone()

    if request_type == "CAP_RESERVATION":
        if not reservation:
            raise LookupError("A local capacity reservation is required before a CAP ticket")
        if reservation["inventory_id"] is None:
            raise LookupError(
                "An inventory-backed regional reservation is required before a CAP ticket"
            )
        project_key = settings.jira_capacity_project_key
        issue_type = settings.jira_capacity_issue_type
        inventory_id = reservation["inventory_id"]
        summary = (
            f"Reserve {float(reservation['requested_tib']):,.1f} TiB for "
            f"{case['company_name']} in {reservation['region']}"
        )
        details = {
            "customer": case["company_name"],
            "ticker": case["ticker"],
            "region": reservation["region"],
            "qfab": reservation["qfab"],
            "service": reservation["service"],
            "vault_type": reservation["vault_type"],
            "tenancy_type": reservation["tenancy_type"],
            "requested_tib": reservation["requested_tib"],
            "available_before_tib": reservation["available_before_tib"],
            "available_after_tib": reservation["available_after_tib"],
            "required_by": reservation["target_date"],
            "reservation_id": reservation["reservation_id"],
            "case_id": case_id,
        }
    else:
        if reservation:
            if not reservation["infrastructure_order_recommended"]:
                raise LookupError("This reservation does not require infrastructure ordering")
            options = capacity_availability(
                case_id, reservation["service"], reservation["vault_type"], 0
            )
            option = next(
                item for item in options if item["qfab"] == reservation["qfab"]
            )
            details = {
                "customer": case["company_name"],
                "ticker": case["ticker"],
                "region": reservation["region"],
                "qfab": reservation["qfab"],
                "service": reservation["service"],
                "vault_type": reservation["vault_type"],
                "tenancy_type": reservation["tenancy_type"],
                "requested_tib": reservation["requested_tib"],
                "available_tib": reservation["available_before_tib"],
                "shortfall_tib": 0,
                "required_by": reservation["target_date"],
                "reservation_id": reservation["reservation_id"],
                "case_id": case_id,
            }
            inventory_id = reservation["inventory_id"]
        else:
            required = ("service", "vault_type", "qfab", "requested_tib", "target_date")
            if any(request.get(field) is None for field in required):
                raise LookupError("Capacity context is required for a HUB ticket")
            options = capacity_availability(
                case_id,
                request["service"],
                request["vault_type"],
                request["requested_tib"],
            )
            option = next(
                (item for item in options if item["qfab"] == request["qfab"]), None
            )
            if not option or option["capacity_sufficient"]:
                raise LookupError("A HUB shortage ticket requires verified insufficient capacity")
            details = {
                "customer": case["company_name"],
                "ticker": case["ticker"],
                "region": case["region"],
                "qfab": option["qfab"],
                "service": option["service"],
                "vault_type": option["vault_type"],
                "tenancy_type": option["tenancy_type"],
                "requested_tib": option["requested_tib"],
                "available_tib": option["available_capacity_tib"],
                "shortfall_tib": option["shortfall_tib"],
                "required_by": request["target_date"],
                "reservation_id": None,
                "case_id": case_id,
            }
            inventory_id = option["inventory_id"]
        order_tib = _recommended_order_tib(option)
        details["recommended_order_tib"] = order_tib
        project_key = settings.jira_hub_project_key
        issue_type = settings.jira_hub_issue_type
        summary = (
            f"Order {float(order_tib):,.1f} TiB infrastructure for "
            f"{details['region']}/{details['qfab']}"
        )

    details["planner_identity"] = request["planner_identity"]
    details["planner_note"] = request.get("note", "")
    description_lines = [
        f"Request type: {request_type}",
        *[f"{key.replace('_', ' ').title()}: {value}" for key, value in details.items()],
    ]
    jira_request_id = uuid4()
    payload = {**details, "description_lines": description_lines}
    with connection() as conn:
        row = conn.execute(
            """insert into capacity_planner.jira_request(
               jira_request_id,case_id,reservation_id,company_id,inventory_id,request_type,
               project_key,issue_type,summary,payload,planner_identity,region,qfab,service,
               vault_type,tenancy_type,requested_tib,target_date)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict(case_id,request_type) do update set updated_at=now()
               returning *, (xmax = 0) created""",
            (
                jira_request_id,
                case_id,
                details.get("reservation_id"),
                case["company_id"],
                inventory_id,
                request_type,
                project_key,
                issue_type,
                summary,
                json.dumps(payload, default=str),
                request["planner_identity"],
                details["region"],
                details["qfab"],
                details["service"],
                details["vault_type"],
                details["tenancy_type"],
                details["requested_tib"],
                details["required_by"],
            ),
        ).fetchone()
    result = dict(row)
    return {**result, "created": bool(result.get("created", True))}


def jira_requests(case_id: str | None = None) -> list[dict]:
    sql = """select j.*,
               coalesce(c.company_name,'Regional capacity request') company_name,
               c.ticker
             from capacity_planner.jira_request j
             left join capacity_planner.company c using(company_id)"""
    params = ()
    if case_id:
        sql += " where j.case_id=%s"
        params = (case_id,)
    sql += " order by j.created_at desc"
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def claim_jira_request(worker_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """select * from capacity_planner.jira_request
               where status in ('QUEUED','RETRY') and available_at <= now()
               order by created_at for update skip locked limit 1"""
        ).fetchone()
        if not row:
            return None
        row = conn.execute(
            """update capacity_planner.jira_request set status='RUNNING',
               attempt_count=attempt_count+1,locked_at=now(),locked_by=%s,updated_at=now()
               where jira_request_id=%s returning *""",
            (worker_id, row["jira_request_id"]),
        ).fetchone()
    return dict(row)


def complete_jira_request(request_id: str, issue: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """update capacity_planner.jira_request set status='COMPLETE',
               jira_issue_key=%s,jira_issue_url=%s,completed_at=now(),last_error=null,
               locked_at=null,locked_by=null,updated_at=now() where jira_request_id=%s""",
            (issue["jira_issue_key"], issue["jira_issue_url"], request_id),
        )


def fail_jira_request(item: dict[str, Any], error: Exception) -> None:
    maximum = get_settings().jira_max_attempts
    status = "FAILED" if item["attempt_count"] >= maximum else "RETRY"
    delay_minutes = min(60, 2 ** item["attempt_count"])
    with connection() as conn:
        conn.execute(
            """update capacity_planner.jira_request set status=%s,last_error=%s,
               available_at=now()+(%s * interval '1 minute'),locked_at=null,locked_by=null,
               updated_at=now() where jira_request_id=%s""",
            (
                status,
                f"{type(error).__name__}: {error}"[:2000],
                delay_minutes,
                item["jira_request_id"],
            ),
        )
