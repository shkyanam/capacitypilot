"""Create append-only capacity-alert scenarios without changing source records."""

import argparse
import json
import uuid
from decimal import Decimal

from capacity_planner.db import connection, migrate


def create_scenarios(count: int, created_by: str) -> list[dict]:
    migrate()
    with connection() as conn:
        candidates = conn.execute(
            """with latest as (
                 select distinct on (cr.company_id)
                   cr.company_id,cr.recommendation
                 from capacity_planner.case_run cr
                 where cr.recommendation is not null
                 order by cr.company_id,cr.created_at desc
               )
               select c.company_id,c.company_name,c.ticker,s.installed_tib
               from capacity_planner.company c
               join capacity_planner.capacity_signal s using(company_id)
               left join latest l using(company_id)
               where s.source_freshness='FRESH'
                 and coalesce((l.recommendation->>'likelihood_pct')::numeric,0) < 80
                 and exists (
                   select 1 from capacity_planner.news_evidence ne
                   where ne.company_id=c.company_id
                     and ne.fetched_at >= now()-interval '24 hours'
                 )
                 and not exists (
                   select 1 from capacity_planner.case_run active
                   where active.company_id=c.company_id
                     and active.status in ('QUEUED','RUNNING','RETRY')
                 )
                 and not exists (
                   select 1
                   from capacity_planner.local_capacity_reservation lr
                   join capacity_planner.case_run reserved using(case_id)
                   where reserved.company_id=c.company_id
                 )
               order by coalesce((l.recommendation->>'likelihood_pct')::numeric,0),
                 c.company_id
               limit %s""",
            (count,),
        ).fetchall()
        created = []
        for candidate in candidates:
            installed = Decimal(candidate["installed_tib"])
            scenario_id = uuid.uuid4()
            case_id = uuid.uuid4()
            scenario = {
                "installed_tib": installed,
                "consumed_tib": (installed * Decimal("0.94")).quantize(
                    Decimal("0.01")
                ),
                "trailing_12m_growth_tib": (installed * Decimal("0.30")).quantize(
                    Decimal("0.01")
                ),
                "prior_expansion_count": 4,
                "avg_prior_expansion_tib": (installed * Decimal("0.20")).quantize(
                    Decimal("0.01")
                ),
                "open_demand_tib": (installed * Decimal("0.25")).quantize(
                    Decimal("0.01")
                ),
                "demand_stage": "COMMITTED",
            }
            reason = "Additional alert workflow validation"
            conn.execute(
                """insert into capacity_planner.capacity_signal_scenario(
                   scenario_id,company_id,installed_tib,consumed_tib,
                   trailing_12m_growth_tib,prior_expansion_count,
                   avg_prior_expansion_tib,open_demand_tib,demand_stage,reason,created_by)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    scenario_id,
                    candidate["company_id"],
                    *scenario.values(),
                    reason,
                    created_by,
                ),
            )
            conn.execute(
                """insert into capacity_planner.case_run(
                   case_id,company_id,scenario_id,status,priority)
                   values (%s,%s,%s,'QUEUED',1)""",
                (case_id, candidate["company_id"], scenario_id),
            )
            conn.execute(
                """insert into capacity_planner.case_event(case_id,event_type,payload)
                   values (%s,'test_scenario_created',%s)""",
                (
                    case_id,
                    json.dumps(
                        {
                            "scenario_id": str(scenario_id),
                            "created_by": created_by,
                            "reason": reason,
                            "scenario": scenario,
                            "source_records_modified": False,
                        },
                        default=str,
                    ),
                ),
            )
            created.append(
                {
                    "case_id": str(case_id),
                    "scenario_id": str(scenario_id),
                    "company_id": candidate["company_id"],
                    "company_name": candidate["company_name"],
                    "ticker": candidate["ticker"],
                }
            )
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=8, choices=range(1, 51))
    parser.add_argument("--created-by", required=True)
    args = parser.parse_args()
    print(json.dumps(create_scenarios(args.count, args.created_by), indent=2))


if __name__ == "__main__":
    main()
