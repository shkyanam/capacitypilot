create table if not exists capacity_planner.planner_forecast_override (
  override_id uuid primary key,
  case_id uuid not null references capacity_planner.case_run(case_id),
  likelihood_pct numeric(5,2) not null check (likelihood_pct between 0 and 100),
  confidence text not null check (confidence in ('LOW','MEDIUM','HIGH')),
  timing_days integer check (timing_days between 0 and 3650),
  capacity_growth_tib numeric(14,2) check (capacity_growth_tib >= 0),
  action text not null check (action in ('PLANNER_REVIEW','MONITOR')),
  note text,
  modified_by text not null,
  created_at timestamptz not null default now()
);

create index if not exists ix_planner_forecast_override_latest
  on capacity_planner.planner_forecast_override(case_id, created_at desc);
