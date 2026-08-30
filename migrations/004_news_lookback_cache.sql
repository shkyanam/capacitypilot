alter table capacity_planner.news_evidence
  add column if not exists lookback_days integer not null default 180;

create index if not exists ix_news_evidence_cache_window
  on capacity_planner.news_evidence(company_id, lookback_days, fetched_at desc);
