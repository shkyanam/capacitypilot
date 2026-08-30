create table if not exists capacity_planner.news_evidence (
  news_id uuid primary key,
  company_id bigint not null references capacity_planner.company(company_id),
  provider text not null,
  external_id text not null,
  title text not null,
  publisher text not null,
  published_at timestamptz not null,
  source_url text not null,
  excerpt text not null,
  categories text[] not null default '{}',
  relevance_score numeric(5,4) not null check (relevance_score between 0 and 1),
  fetched_at timestamptz not null default now(),
  metadata jsonb not null default '{}',
  unique(company_id, provider, external_id)
);
create index if not exists ix_news_evidence_company_date
  on capacity_planner.news_evidence(company_id, published_at desc);
