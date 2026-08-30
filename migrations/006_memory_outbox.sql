create table if not exists capacity_planner.memory_outbox (
  outbox_id uuid primary key,
  company_id bigint not null references capacity_planner.company(company_id),
  event_type text not null check (event_type in ('PLANNER_DECISION','PREDICTION_OUTCOME')),
  payload jsonb not null,
  status text not null default 'QUEUED'
    check (status in ('QUEUED','RUNNING','RETRY','COMPLETE','FAILED')),
  attempt_count integer not null default 0,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists ix_memory_outbox_queue
  on capacity_planner.memory_outbox(status, available_at, created_at);
