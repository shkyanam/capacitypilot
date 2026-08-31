create table if not exists capacity_planner.news_semantic_comparison_run (
  run_id uuid primary key,
  customer_limit integer not null check (customer_limit between 1 and 1000),
  created_at timestamptz not null default now()
);

alter table capacity_planner.news_ingestion_job
  add column if not exists comparison_run_id uuid
    references capacity_planner.news_semantic_comparison_run(run_id);

create table if not exists capacity_planner.news_evidence_baseline (
  baseline_id bigint generated always as identity primary key,
  run_id uuid not null references capacity_planner.news_semantic_comparison_run(run_id),
  company_id bigint not null references capacity_planner.company(company_id),
  provider text not null,
  external_id text not null,
  title text not null,
  source_url text not null,
  excerpt text not null,
  categories text[] not null default '{}',
  relevance_score numeric(5,4) not null check (relevance_score between 0 and 1),
  metadata jsonb not null default '{}',
  captured_at timestamptz not null default now(),
  unique(run_id, company_id, provider, external_id)
);

create index if not exists ix_news_evidence_baseline_run_company
  on capacity_planner.news_evidence_baseline(run_id, company_id);
