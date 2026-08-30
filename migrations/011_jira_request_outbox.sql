create table if not exists capacity_planner.jira_request (
  jira_request_id uuid primary key,
  case_id uuid not null references capacity_planner.case_run(case_id),
  reservation_id uuid references capacity_planner.local_capacity_reservation(reservation_id),
  company_id bigint not null references capacity_planner.company(company_id),
  inventory_id bigint references capacity_planner.capacity_inventory(inventory_id),
  request_type text not null
    check (request_type in ('CAP_RESERVATION','HUB_INFRASTRUCTURE')),
  project_key text not null,
  issue_type text not null,
  summary text not null,
  payload jsonb not null,
  status text not null default 'QUEUED'
    check (status in ('QUEUED','RUNNING','RETRY','COMPLETE','FAILED')),
  attempt_count integer not null default 0,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  last_error text,
  jira_issue_key text,
  jira_issue_url text,
  planner_identity text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  unique(case_id,request_type)
);

create index if not exists ix_jira_request_queue
  on capacity_planner.jira_request(status,available_at,created_at);
