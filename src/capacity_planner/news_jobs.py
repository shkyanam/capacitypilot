import json
from typing import Any

from .config import get_settings
from .db import connection, migrate


def enqueue_all() -> int:
    settings = get_settings()
    with connection() as conn:
        cursor = conn.execute(
            """insert into capacity_planner.news_ingestion_job(company_id,status)
               select company_id,'QUEUED' from capacity_planner.company
               on conflict(company_id) do update set
                 status='QUEUED',attempt_count=0,available_at=now(),locked_at=null,locked_by=null,
                 last_error=null,updated_at=now(),completed_at=null
               where capacity_planner.news_ingestion_job.status in ('COMPLETE','NO_EVIDENCE','FAILED')
                 and capacity_planner.news_ingestion_job.updated_at
                     < now()-(%s * interval '1 hour')""",
            (settings.news_bulk_refresh_hours,),
        )
    return cursor.rowcount


def claim_job(worker_id: str) -> dict[str, Any] | None:
    settings = get_settings()
    with connection() as conn:
        row = conn.execute(
            """select * from capacity_planner.news_ingestion_job
               where status in ('QUEUED','RETRY') and available_at <= now()
                 and attempt_count < %s
               order by available_at,company_id for update skip locked limit 1""",
            (settings.news_bulk_max_attempts,),
        ).fetchone()
        if not row:
            return None
        row = conn.execute(
            """update capacity_planner.news_ingestion_job
               set status='RUNNING',attempt_count=attempt_count+1,locked_at=now(),locked_by=%s,
                   updated_at=now() where company_id=%s returning *""",
            (worker_id, row["company_id"]),
        ).fetchone()
    return dict(row)


def finish_job(job: dict[str, Any], result: dict[str, Any]) -> None:
    source_status = result["status"]
    if source_status in {"AVAILABLE", "DEGRADED"}:
        status = "COMPLETE"
    elif source_status == "NO_RELEVANT_EVIDENCE":
        status = "NO_EVIDENCE"
    else:
        status = (
            "FAILED"
            if job["attempt_count"] >= get_settings().news_bulk_max_attempts
            else "RETRY"
        )
    errors = result.get("errors", [])
    delay_minutes = min(60, 2 ** job["attempt_count"])
    with connection() as conn:
        conn.execute(
            """update capacity_planner.news_ingestion_job set status=%s,evidence_count=%s,
               last_error=%s,available_at=case when %s='RETRY'
                 then now()+(%s * interval '1 minute') else available_at end,
               locked_at=null,locked_by=null,updated_at=now(),
               completed_at=case when %s in ('COMPLETE','NO_EVIDENCE') then now() else completed_at end
               where company_id=%s""",
            (
                status,
                len(result.get("items", [])),
                json.dumps(errors)[:2000] if errors else None,
                status,
                delay_minutes,
                status,
                job["company_id"],
            ),
        )


def fail_job(job: dict[str, Any], error: Exception) -> None:
    terminal = job["attempt_count"] >= get_settings().news_bulk_max_attempts
    delay_minutes = min(60, 2 ** job["attempt_count"])
    with connection() as conn:
        conn.execute(
            """update capacity_planner.news_ingestion_job set status=%s,last_error=%s,
               available_at=now()+(%s * interval '1 minute'),locked_at=null,locked_by=null,
               updated_at=now() where company_id=%s""",
            (
                "FAILED" if terminal else "RETRY",
                f"{type(error).__name__}: {error}"[:2000],
                delay_minutes,
                job["company_id"],
            ),
        )


def recover_stale_jobs() -> int:
    with connection() as conn:
        cursor = conn.execute(
            """update capacity_planner.news_ingestion_job
               set status='RETRY',locked_at=null,locked_by=null,available_at=now(),
                   last_error='Worker lease expired; job returned to retry queue',updated_at=now()
               where status='RUNNING'
                 and locked_at < now()-(%s * interval '1 minute')""",
            (get_settings().stale_case_minutes,),
        )
    return cursor.rowcount


def status_counts() -> dict[str, Any]:
    with connection() as conn:
        rows = conn.execute(
            "select status,count(*) count from capacity_planner.news_ingestion_job group by status"
        ).fetchall()
        total_evidence = conn.execute(
            "select count(*) count from capacity_planner.news_evidence"
        ).fetchone()["count"]
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "jobs": counts,
        "total_jobs": sum(counts.values()),
        "processed_jobs": counts.get("COMPLETE", 0) + counts.get("NO_EVIDENCE", 0),
        "evidence_records": total_evidence,
    }


def enqueue_main() -> None:
    migrate()
    print(f"Queued or refreshed {enqueue_all()} company news-ingestion jobs")


if __name__ == "__main__":
    enqueue_main()
