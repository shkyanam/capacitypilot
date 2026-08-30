create table if not exists capacity_planner.local_capacity_reservation (
  reservation_id uuid primary key,
  case_id uuid not null unique references capacity_planner.case_run(case_id),
  company_id bigint not null references capacity_planner.company(company_id),
  requested_tib numeric(14,2) not null check (requested_tib > 0),
  target_date date not null,
  service text not null check (service = 'Storage Capacity'),
  vault_type text not null
    check (vault_type in ('Standard','High Performance','Ultra Performance',
      'System Standard','System Critical','Replication','General Purpose')),
  tenancy_type text not null check (tenancy_type in ('Shared','Dedicated','Replicated')),
  region text not null,
  qfab text,
  status text not null default 'LOCAL_RESERVED' check (status in ('LOCAL_RESERVED','CANCELLED')),
  source text not null default 'LOCAL_PLANNING_ONLY',
  planner_identity text not null,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint ck_local_reservation_service_vault check (
    service = 'Storage Capacity'
  )
);

create index if not exists ix_local_capacity_reservation_status
  on capacity_planner.local_capacity_reservation(status, target_date);
