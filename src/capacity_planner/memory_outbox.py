import json
from typing import Any
from uuid import uuid4

from .config import get_settings
from .db import connection


def claim_memory(worker_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """select * from capacity_planner.memory_outbox
               where status in ('QUEUED','RETRY') and available_at <= now()
               order by created_at for update skip locked limit 1"""
        ).fetchone()
        if not row:
            return None
        row = conn.execute(
            """update capacity_planner.memory_outbox set status='RUNNING',
               attempt_count=attempt_count+1,locked_at=now(),locked_by=%s,updated_at=now()
               where outbox_id=%s returning *""",
            (worker_id, row["outbox_id"]),
        ).fetchone()
    return dict(row)


def complete_memory(outbox_id: str) -> None:
    with connection() as conn:
        conn.execute(
            """update capacity_planner.memory_outbox set status='COMPLETE',completed_at=now(),
               locked_at=null,locked_by=null,last_error=null,updated_at=now() where outbox_id=%s""",
            (outbox_id,),
        )


def fail_memory(item: dict[str, Any], error: Exception) -> None:
    maximum = get_settings().mem0_max_attempts
    status = "FAILED" if item["attempt_count"] >= maximum else "RETRY"
    delay_minutes = min(60, 2 ** item["attempt_count"])
    with connection() as conn:
        conn.execute(
            """update capacity_planner.memory_outbox set status=%s,last_error=%s,
               available_at=now()+(%s * interval '1 minute'),locked_at=null,locked_by=null,
               updated_at=now() where outbox_id=%s""",
            (
                status,
                f"{type(error).__name__}: {error}"[:2000],
                delay_minutes,
                item["outbox_id"],
            ),
        )


def enqueue_planner_decision(
    conn,
    *,
    company_id: int,
    case_id: str,
    decision: str,
    recommendation: dict[str, Any],
    planner_comment: str = "",
) -> None:
    likelihood = float(recommendation.get("likelihood_pct", 0))
    band = "HIGH" if likelihood >= 80 else "MEDIUM" if likelihood >= 50 else "LOW"
    payload = {
        "case_id": case_id,
        "decision": decision,
        "likelihood_band": band,
        "confidence": recommendation.get("confidence", "UNKNOWN"),
    }
    comment = " ".join(planner_comment.split())[:500]
    if comment:
        payload["planner_comment"] = comment
    conn.execute(
        """insert into capacity_planner.memory_outbox(
           outbox_id,company_id,event_type,payload) values (%s,%s,'PLANNER_DECISION',%s)""",
        (uuid4(), company_id, json.dumps(payload)),
    )
