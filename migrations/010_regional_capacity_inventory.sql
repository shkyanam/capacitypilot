alter table capacity_planner.company
  add column if not exists region text;

update capacity_planner.company
set region = case mod(company_id, 5)
  when 0 then 'us-ashburn-1'
  when 1 then 'us-phoenix-1'
  when 2 then 'eu-frankfurt-1'
  when 3 then 'uk-london-1'
  else 'ap-tokyo-1'
end
where region is null;

alter table capacity_planner.company
  alter column region set default 'us-ashburn-1',
  alter column region set not null;

create table if not exists capacity_planner.capacity_inventory (
  inventory_id bigint generated always as identity primary key,
  region text not null,
  qfab text not null,
  service text not null check (service = 'Storage Capacity'),
  vault_type text not null
    check (vault_type in ('Standard','High Performance','Ultra Performance',
      'System Standard','System Critical','Replication','General Purpose')),
  tenancy_type text not null check (tenancy_type in ('Shared','Dedicated','Replicated')),
  usable_capacity_tib numeric(14,2) not null check (usable_capacity_tib > 0),
  allocated_capacity_tib numeric(14,2) not null
    check (allocated_capacity_tib >= 0 and allocated_capacity_tib <= usable_capacity_tib),
  freshness_status text not null default 'FRESH'
    check (freshness_status in ('FRESH','STALE','MISSING')),
  data_classification text not null default 'SYNTHETIC_DEMO',
  source_updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(region,qfab,service,vault_type),
  constraint ck_capacity_inventory_service_vault check (
    service = 'Storage Capacity'
  )
);

with regions(region,region_rank) as (
  values
    ('us-ashburn-1',1),
    ('us-phoenix-1',2),
    ('eu-frankfurt-1',3),
    ('uk-london-1',4),
    ('ap-tokyo-1',5)
), qfabs(qfab,qfab_rank) as (
  values ('QFAB-A',1),('QFAB-B',2)
), mappings(service,vault_type,tenancy_type,mapping_rank) as (
  values
    ('Storage Capacity','High Performance','Shared',1),
    ('Storage Capacity','Ultra Performance','Shared',2),
    ('Storage Capacity','System Critical','Dedicated',3),
    ('Storage Capacity','Standard','Shared',4),
    ('Storage Capacity','System Standard','Dedicated',5),
    ('Storage Capacity','Replication','Replicated',6),
    ('Storage Capacity','General Purpose','Replicated',7)
), pools as (
  select r.region,q.qfab,m.service,m.vault_type,m.tenancy_type,
         (1800 + r.region_rank * 350 + q.qfab_rank * 200)::numeric(14,2) usable,
         (0.45 + 0.08 * mod(r.region_rank + q.qfab_rank + m.mapping_rank,6))::numeric ratio
  from regions r cross join qfabs q cross join mappings m
)
insert into capacity_planner.capacity_inventory(
  region,qfab,service,vault_type,tenancy_type,usable_capacity_tib,
  allocated_capacity_tib,freshness_status,data_classification)
select region,qfab,service,vault_type,tenancy_type,usable,
       round(usable * ratio,2),'FRESH','SYNTHETIC_DEMO'
from pools
on conflict(region,qfab,service,vault_type) do nothing;

create index if not exists ix_capacity_inventory_lookup
  on capacity_planner.capacity_inventory(region,service,vault_type,qfab);

alter table capacity_planner.local_capacity_reservation
  add column if not exists inventory_id bigint
    references capacity_planner.capacity_inventory(inventory_id),
  add column if not exists available_before_tib numeric(14,2),
  add column if not exists available_after_tib numeric(14,2),
  add column if not exists post_reservation_allocation_pct numeric(5,2),
  add column if not exists infrastructure_order_recommended boolean not null default false;
