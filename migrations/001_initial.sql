create schema if not exists capacity_planner;

create table if not exists capacity_planner.company (
  company_id bigint generated always as identity primary key,
  sec_cik bigint not null unique,
  company_name text not null,
  ticker text not null,
  exchange text,
  identity_source text not null default 'SEC_EDGAR',
  identity_is_real boolean not null default true,
  loaded_at timestamptz not null default now()
);

create table if not exists capacity_planner.capacity_signal (
  company_id bigint primary key references capacity_planner.company(company_id),
  installed_tib numeric(14,2) not null check (installed_tib > 0),
  consumed_tib numeric(14,2) not null check (consumed_tib between 0 and installed_tib),
  trailing_12m_growth_tib numeric(14,2) not null,
  prior_expansion_count integer not null check (prior_expansion_count >= 0),
  avg_prior_expansion_tib numeric(14,2) not null,
  open_demand_tib numeric(14,2) not null,
  demand_stage text not null,
  news_event_count integer not null default 0,
  news_relevance numeric(5,2) not null default 0,
  news_summary text not null default 'No public signal evaluated',
  source_freshness text not null check (source_freshness in ('FRESH','STALE','MISSING')),
  generated_at timestamptz not null default now(),
  data_classification text not null default 'SYNTHETIC_DEMO'
);

alter table capacity_planner.capacity_signal add column if not exists news_event_count integer not null default 0;
alter table capacity_planner.capacity_signal add column if not exists news_relevance numeric(5,2) not null default 0;
alter table capacity_planner.capacity_signal add column if not exists news_summary text not null default 'No public signal evaluated';

create table if not exists capacity_planner.case_run (
  case_id uuid primary key,
  company_id bigint not null references capacity_planner.company(company_id),
  status text not null check (status in ('QUEUED','RUNNING','RETRY','REVIEW_REQUIRED','COMPLETE','FAILED')),
  attempt_count integer not null default 0,
  locked_at timestamptz,
  locked_by text,
  last_error text,
  recommendation jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists ix_case_run_queue on capacity_planner.case_run(status, created_at);

create table if not exists capacity_planner.case_event (
  event_id bigint generated always as identity primary key,
  case_id uuid not null references capacity_planner.case_run(case_id),
  event_type text not null,
  payload jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists capacity_planner.planner_decision (
  decision_id bigint generated always as identity primary key,
  case_id uuid not null references capacity_planner.case_run(case_id),
  decision text not null check (decision in ('APPROVE_REVIEW','MONITOR','REJECT_INVESTIGATE')),
  note text,
  decided_by text not null,
  decided_at timestamptz not null default now()
);

create table if not exists capacity_planner.prediction_outcome (
  case_id uuid primary key references capacity_planner.case_run(case_id),
  expanded boolean not null,
  actual_expansion_tib numeric(14,2),
  actual_expansion_date date,
  recorded_at timestamptz not null default now()
);
