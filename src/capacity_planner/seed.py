import hashlib
import random

import httpx

from .config import get_settings
from .db import connection, migrate

SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CUSTOMER_REGIONS = (
    "us-ashburn-1",
    "us-phoenix-1",
    "eu-frankfurt-1",
    "uk-london-1",
    "ap-tokyo-1",
)


def customer_region(cik: int) -> str:
    """Assign a stable synthetic home region for demonstration customers."""
    digest = int(hashlib.sha256(f"region:{cik}".encode()).hexdigest()[:12], 16)
    return CUSTOMER_REGIONS[digest % len(CUSTOMER_REGIONS)]


def synthetic_signal(cik: int) -> tuple:
    """Deterministic demo signals; never represented as actual company operations."""
    rng = random.Random(int(hashlib.sha256(str(cik).encode()).hexdigest()[:12], 16))
    installed = rng.choice([250, 500, 750, 1000, 1500, 2000, 3000])
    utilization = rng.uniform(0.45, 0.98)
    return (
        installed,
        round(installed * utilization, 2),
        round(rng.uniform(-30, 400), 2),
        rng.randint(0, 8),
        round(rng.uniform(25, 500), 2),
        round(rng.choice([0, 0, 50, 100, 250, 500]), 2),
        rng.choice(["NONE", "DISCOVERY", "QUALIFIED", "COMMITTED"]),
        rng.randint(0, 5),
        round(rng.uniform(0, 1), 2),
        rng.choice([
            "Synthetic acquisition or growth indicator",
            "Synthetic data-center expansion indicator",
            "No synthetic public-demand indicator",
        ]),
        rng.choices(["FRESH", "STALE", "MISSING"], weights=[92, 6, 2])[0],
    )


def fetch_companies(limit: int = 1000) -> list[dict]:
    settings = get_settings()
    response = httpx.get(SEC_URL, headers={"User-Agent": settings.sec_user_agent}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    columns = payload["fields"]
    records = [dict(zip(columns, row, strict=True)) for row in payload["data"]]
    eligible = [r for r in records if r.get("ticker") and r.get("exchange")]
    return eligible[:limit]


def load(limit: int = 1000) -> int:
    migrate()
    records = fetch_companies(limit)
    with connection() as conn:
        for record in records:
            company = conn.execute(
                """insert into capacity_planner.company(
                   sec_cik,company_name,ticker,exchange,region)
                   values (%s,%s,%s,%s,%s) on conflict(sec_cik) do update set
                   company_name=excluded.company_name,ticker=excluded.ticker,
                   exchange=excluded.exchange,region=excluded.region
                   returning company_id""",
                (
                    record["cik"],
                    record["name"],
                    record["ticker"],
                    record["exchange"],
                    customer_region(record["cik"]),
                ),
            ).fetchone()
            values = synthetic_signal(record["cik"])
            conn.execute(
                """insert into capacity_planner.capacity_signal(
                   company_id,installed_tib,consumed_tib,trailing_12m_growth_tib,
                   prior_expansion_count,avg_prior_expansion_tib,open_demand_tib,demand_stage,
                   news_event_count,news_relevance,news_summary,source_freshness)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(company_id) do update set installed_tib=excluded.installed_tib,
                   consumed_tib=excluded.consumed_tib,trailing_12m_growth_tib=excluded.trailing_12m_growth_tib,
                   prior_expansion_count=excluded.prior_expansion_count,avg_prior_expansion_tib=excluded.avg_prior_expansion_tib,
                   open_demand_tib=excluded.open_demand_tib,demand_stage=excluded.demand_stage,
                   news_event_count=excluded.news_event_count,news_relevance=excluded.news_relevance,
                   news_summary=excluded.news_summary,
                   source_freshness=excluded.source_freshness,generated_at=now()""",
                (company["company_id"], *values),
            )
    return len(records)


def main() -> None:
    count = load(1000)
    print(f"Loaded {count} real SEC company identities with synthetic planning signals")


if __name__ == "__main__":
    main()
