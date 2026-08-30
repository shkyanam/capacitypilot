create table if not exists capacity_planner.capacity_signal_scenario (
  scenario_id uuid primary key,
  company_id bigint not null references capacity_planner.company(company_id),
  installed_tib numeric(14,2) not null check (installed_tib > 0),
  consumed_tib numeric(14,2) not null check (consumed_tib between 0 and installed_tib),
  trailing_12m_growth_tib numeric(14,2) not null,
  prior_expansion_count integer not null check (prior_expansion_count >= 0),
  avg_prior_expansion_tib numeric(14,2) not null,
  open_demand_tib numeric(14,2) not null check (open_demand_tib >= 0),
  demand_stage text not null,
  reason text not null,
  created_by text not null,
  created_at timestamptz not null default now()
);

alter table capacity_planner.case_run
  add column if not exists scenario_id uuid
    references capacity_planner.capacity_signal_scenario(scenario_id);

create index if not exists ix_case_run_scenario
  on capacity_planner.case_run(scenario_id)
  where scenario_id is not null;
