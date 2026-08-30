alter table capacity_planner.case_run
  add column if not exists priority smallint not null default 100
  check (priority between 1 and 1000);

-- A queued case for a company that already has a recommendation is an ad hoc
-- planner rerun. Promote existing reruns when this migration is first applied.
update capacity_planner.case_run queued
set priority = 10,
    updated_at = now()
where queued.status in ('QUEUED','RETRY')
  and queued.priority <> 10
  and exists (
    select 1
    from capacity_planner.case_run prior
    where prior.company_id = queued.company_id
      and prior.case_id <> queued.case_id
      and prior.recommendation is not null
  );

create index if not exists ix_case_run_claim_priority
  on capacity_planner.case_run(priority, created_at)
  where status in ('QUEUED','RETRY');
