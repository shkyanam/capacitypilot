create index if not exists ix_case_run_company_created
  on capacity_planner.case_run(company_id, created_at desc);

create index if not exists ix_planner_decision_case_decided
  on capacity_planner.planner_decision(case_id, decided_at desc);

create index if not exists ix_local_reservation_company_status
  on capacity_planner.local_capacity_reservation(company_id, status);
