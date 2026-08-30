create unique index if not exists ux_case_run_active_company
  on capacity_planner.case_run(company_id)
  where status in ('QUEUED','RUNNING','RETRY');
