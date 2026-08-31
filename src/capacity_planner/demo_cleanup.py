"""Safely reduce the local demonstration portfolio while retaining referential integrity."""

from argparse import ArgumentParser

from .db import connection, migrate


def prune_to_customer_limit(limit: int) -> dict[str, int]:
    """Keep the earliest customer IDs and delete dependent demo records for the remainder."""
    if limit < 1:
        raise ValueError("limit must be positive")
    with connection() as conn:
        active = conn.execute(
            """with retained as (
                   select company_id from capacity_planner.company order by company_id limit %s
               )
               select
                 (select count(*) from capacity_planner.news_ingestion_job
                  where status='RUNNING' and company_id not in (select company_id from retained)) news_jobs,
                 (select count(*) from capacity_planner.case_run
                  where status='RUNNING' and company_id not in (select company_id from retained)) cases""",
            (limit,),
        ).fetchone()
        if active["news_jobs"] or active["cases"]:
            raise RuntimeError(
                "Stop active workers before pruning: "
                f"{active['news_jobs']} news job(s) and {active['cases']} case(s) target removal"
            )
        before = conn.execute("select count(*) count from capacity_planner.company").fetchone()["count"]
        target = """company_id not in (
            select company_id from capacity_planner.company order by company_id limit %s
        )"""
        # Delete dependent records first. All operations share this one transaction.
        statements = (
            "delete from capacity_planner.news_evidence_baseline where " + target,
            "delete from capacity_planner.news_ingestion_job where " + target,
            "delete from capacity_planner.jira_request where company_id is not null and " + target,
            "delete from capacity_planner.memory_outbox where " + target,
            "delete from capacity_planner.news_evidence where " + target,
            "delete from capacity_planner.capacity_signal where " + target,
            "delete from capacity_planner.capacity_signal_scenario where " + target,
        )
        # Case-owned records need a subquery because they reference case_run rather than company.
        case_target = "case_id in (select case_id from capacity_planner.case_run where " + target + ")"
        for statement in (
            "delete from capacity_planner.jira_request where " + case_target,
            "delete from capacity_planner.planner_forecast_override where " + case_target,
            "delete from capacity_planner.planner_decision where " + case_target,
            "delete from capacity_planner.prediction_outcome where " + case_target,
            "delete from capacity_planner.case_event where " + case_target,
            "delete from capacity_planner.local_capacity_reservation where " + case_target,
            "delete from capacity_planner.case_run where " + target,
            *statements,
            "delete from capacity_planner.company where " + target,
        ):
            conn.execute(statement, (limit,))
        after = conn.execute("select count(*) count from capacity_planner.company").fetchone()["count"]
    return {"before": before, "after": after, "removed": before - after}


def main() -> None:
    parser = ArgumentParser(description="Prune the local CapacityPilot demonstration portfolio")
    parser.add_argument("--keep", type=int, default=100, help="Customers to retain (default: 100)")
    parser.add_argument(
        "--confirm-prune",
        action="store_true",
        help="Required acknowledgement because dependent demo records are deleted",
    )
    args = parser.parse_args()
    if not args.confirm_prune:
        raise SystemExit("Refusing to prune without --confirm-prune")
    migrate()
    result = prune_to_customer_limit(args.keep)
    print(
        f"Retained {result['after']} customers; removed {result['removed']} of "
        f"{result['before']} local demonstration customers"
    )


if __name__ == "__main__":
    main()
