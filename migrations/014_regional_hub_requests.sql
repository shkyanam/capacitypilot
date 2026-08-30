alter table capacity_planner.jira_request
  alter column case_id drop not null,
  alter column company_id drop not null;

alter table capacity_planner.jira_request
  add column if not exists region text,
  add column if not exists qfab text,
  add column if not exists service text,
  add column if not exists vault_type text,
  add column if not exists tenancy_type text,
  add column if not exists requested_tib numeric(18,2),
  add column if not exists target_date date;

update capacity_planner.jira_request
set region = coalesce(region, payload->>'region'),
    qfab = coalesce(qfab, payload->>'qfab'),
    service = coalesce(service, payload->>'service'),
    vault_type = coalesce(vault_type, payload->>'vault_type'),
    tenancy_type = coalesce(tenancy_type, payload->>'tenancy_type'),
    requested_tib = coalesce(requested_tib, nullif(payload->>'requested_tib','')::numeric),
    target_date = coalesce(target_date, nullif(payload->>'required_by','')::date);

alter table capacity_planner.jira_request
  drop constraint if exists ck_jira_request_customer_scope;

alter table capacity_planner.jira_request
  add constraint ck_jira_request_customer_scope check (
    request_type = 'HUB_INFRASTRUCTURE'
    or (case_id is not null and company_id is not null)
  );

create unique index if not exists ux_jira_request_open_regional_hub
  on capacity_planner.jira_request(region,qfab,service,vault_type)
  where case_id is null
    and request_type = 'HUB_INFRASTRUCTURE'
    and status in ('QUEUED','RUNNING','RETRY');
