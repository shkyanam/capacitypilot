create table if not exists capacity_planner.news_ingestion_job (
  company_id bigint primary key references capacity_planner.company(company_id),
  status text not null check (
    status in ('QUEUED','RUNNING','RETRY','COMPLETE','NO_EVIDENCE','FAILED')
  ),
  attempt_count integer not null default 0,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  evidence_count integer,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists ix_news_ingestion_job_queue
  on capacity_planner.news_ingestion_job(status, available_at, updated_at);
