import json
from argparse import ArgumentParser
from typing import Any
from uuid import uuid4

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


def enqueue_limited(limit: int, *, force: bool = False) -> tuple[int, str]:
    """Snapshot a bounded baseline and queue it; force never interrupts an active lease."""
    if limit < 1:
        raise ValueError("limit must be positive")
    settings = get_settings()
    run_id = str(uuid4())
    with connection() as conn:
        conn.execute(
            """insert into capacity_planner.news_semantic_comparison_run(run_id,customer_limit)
               values (%s,%s)""",
            (run_id, limit),
        )
        conn.execute(
            """with selected_companies as (
                   select company_id from capacity_planner.company order by company_id limit %s
               )
               insert into capacity_planner.news_evidence_baseline(
                 run_id,company_id,provider,external_id,title,source_url,excerpt,categories,
                 relevance_score,metadata)
               select %s,e.company_id,e.provider,e.external_id,e.title,e.source_url,e.excerpt,
                 e.categories,e.relevance_score,e.metadata
               from capacity_planner.news_evidence e
               join selected_companies s on s.company_id=e.company_id
               on conflict(run_id,company_id,provider,external_id) do nothing""",
            (limit, run_id),
        )
        cursor = conn.execute(
            """with selected_companies as (
                   select company_id from capacity_planner.company order by company_id limit %s
               )
               insert into capacity_planner.news_ingestion_job(company_id,status,comparison_run_id)
               select company_id,'QUEUED',%s from selected_companies
               on conflict(company_id) do update set
                 status='QUEUED',attempt_count=0,available_at=now(),locked_at=null,locked_by=null,
                 last_error=null,updated_at=now(),completed_at=null,
                 comparison_run_id=excluded.comparison_run_id
               where capacity_planner.news_ingestion_job.status <> 'RUNNING'
                 and (
                   %s
                   or (
                     capacity_planner.news_ingestion_job.status in ('COMPLETE','NO_EVIDENCE','FAILED')
                     and capacity_planner.news_ingestion_job.updated_at
                         < now()-(%s * interval '1 hour')
                   )
                 )""",
            (limit, run_id, force, settings.news_bulk_refresh_hours),
        )
    return cursor.rowcount, run_id


def latest_comparison() -> dict[str, Any] | None:
    """Return a named, evidence-level lexical-versus-semantic comparison for the latest run."""
    with connection() as conn:
        run = conn.execute(
            """select run_id,customer_limit,created_at
               from capacity_planner.news_semantic_comparison_run
               order by created_at desc limit 1"""
        ).fetchone()
        if not run:
            return None
        rows = conn.execute(
            """select c.company_name,c.ticker,b.provider,b.source_url,
                 b.categories before_categories,b.excerpt before_excerpt,
                 e.categories after_categories,e.excerpt after_excerpt,
                 coalesce(e.metadata->'semantic_retrieval'->'matches','[]'::jsonb) semantic_matches,
                 case
                   when e.news_id is null then 'PENDING_REFRESH'
                   when not (e.metadata ? 'semantic_retrieval') then 'NOT_EVALUATED'
                   when jsonb_array_length(coalesce(e.metadata->'semantic_retrieval'->'matches',
                     '[]'::jsonb)) > 0 then 'MATCH_FOUND'
                   else 'NO_MATCH_FOUND'
                 end semantic_status
               from capacity_planner.news_evidence_baseline b
               join capacity_planner.company c on c.company_id=b.company_id
               left join capacity_planner.news_evidence e
                 on e.company_id=b.company_id and e.provider=b.provider and e.external_id=b.external_id
               where b.run_id=%s
               order by c.company_name,b.provider""",
            (run["run_id"],),
        ).fetchall()
    values = [dict(row) for row in rows]
    return {
        **dict(run),
        "rows": values,
        "semantic_summary": {
            "evaluated": sum(row["semantic_status"] in {"MATCH_FOUND", "NO_MATCH_FOUND"} for row in values),
            "matches": sum(row["semantic_status"] == "MATCH_FOUND" for row in values),
            "pending": sum(row["semantic_status"] == "PENDING_REFRESH" for row in values),
        },
    }


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
    parser = ArgumentParser(description="Queue bounded SEC/news ingestion work")
    parser.add_argument(
        "--limit",
        type=int,
        default=get_settings().news_bulk_company_limit,
        help="Maximum customers to queue (default: NEWS_BULK_COMPANY_LIMIT)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Requeue completed or no-evidence jobs immediately; never interrupts RUNNING jobs",
    )
    args = parser.parse_args()
    queued, run_id = enqueue_limited(args.limit, force=args.force)
    print(
        f"Queued or refreshed {queued} of the first {args.limit} customer news-ingestion jobs "
        f"(comparison run {run_id})"
    )


if __name__ == "__main__":
    enqueue_main()
