create table if not exists capacity_planner.slack_alert_outbox (
  alert_id uuid primary key,
  dedupe_key text not null unique,
  alert_type text not null default 'CAPACITY_DIGEST'
    check (alert_type in ('CAPACITY_DIGEST')),
  payload jsonb not null,
  status text not null default 'QUEUED'
    check (status in ('QUEUED','RUNNING','RETRY','COMPLETE','FAILED')),
  attempt_count integer not null default 0,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  last_error text,
  slack_channel text,
  slack_message_ts text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists ix_slack_alert_outbox_queue
  on capacity_planner.slack_alert_outbox(status,available_at,created_at);
