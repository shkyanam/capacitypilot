import hashlib
import json
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import get_settings
from .db import connection

SIGNALS = {
    "acquisition": ("acquisition", "acquire", "merger", "business combination"),
    "growth_plan": ("expansion", "growth plan", "grow our", "increased demand"),
    "data_center": ("data center", "datacenter", "cloud infrastructure"),
    "capacity_investment": ("capital investment", "capacity expansion", "new facility"),
    "geographic_expansion": ("new region", "new market", "international expansion"),
}
RELEVANT_FORMS = {"8-K", "10-K", "10-Q", "6-K", "20-F", "S-4", "DEFM14A"}
_SEC_RATE_LOCK = threading.Lock()
_SEC_LAST_REQUEST_AT = 0.0


def classify(text: str) -> tuple[list[str], float]:
    normalized = re.sub(r"\s+", " ", text).lower()
    categories = [name for name, terms in SIGNALS.items() if any(term in normalized for term in terms)]
    return categories, min(1.0, round(0.2 + 0.2 * len(categories), 2)) if categories else 0.1


def extract_excerpt(text: str, limit: int = 700) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    lowered = cleaned.lower()
    positions = [lowered.find(term) for terms in SIGNALS.values() for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    return cleaned[start : start + limit]


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)
def _get_json(url: str, headers: dict[str, str], params: dict | None = None) -> dict:
    response = httpx.get(url, headers=headers, params=params, timeout=45, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def _sec_throttle() -> None:
    global _SEC_LAST_REQUEST_AT
    with _SEC_RATE_LOCK:
        interval = get_settings().sec_min_request_interval_seconds
        remaining = interval - (time.monotonic() - _SEC_LAST_REQUEST_AT)
        if remaining > 0:
            time.sleep(remaining)
        _SEC_LAST_REQUEST_AT = time.monotonic()


def _sec_get_json(url: str, headers: dict[str, str]) -> dict:
    _sec_throttle()
    return _get_json(url, headers)


def _sec_get_text(url: str, headers: dict[str, str]) -> str:
    _sec_throttle()
    response = httpx.get(url, headers=headers, timeout=45, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _company(company_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "select company_id,company_name,sec_cik,ticker from capacity_planner.company where company_id=%s",
            (company_id,),
        ).fetchone()
    if not row:
        raise LookupError(f"Company not found: {company_id}")
    return dict(row)


def _sec_evidence(company: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    headers = {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
    cik = f"{company['sec_cik']:010d}"
    payload = _sec_get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", headers)
    recent = payload.get("filings", {}).get("recent", {})
    rows = [dict(zip(recent.keys(), values, strict=False)) for values in zip(*recent.values())]
    cutoff = datetime.now(UTC).date() - timedelta(days=settings.news_lookback_days)
    selected = [
        row
        for row in rows
        if row.get("form") in RELEVANT_FORMS
        and row.get("filingDate")
        and datetime.fromisoformat(row["filingDate"]).date() >= cutoff
    ][: settings.sec_max_filings_per_case]
    evidence = []
    for row in selected:
        accession = row["accessionNumber"]
        accession_compact = accession.replace("-", "")
        document = row.get("primaryDocument", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{company['sec_cik']}/"
            f"{accession_compact}/{document}"
        )
        text = BeautifulSoup(_sec_get_text(url, headers), "html.parser").get_text(" ", strip=True)
        excerpt = extract_excerpt(text)
        categories, relevance = classify(excerpt)
        evidence.append(
            {
                "provider": "SEC_EDGAR",
                "external_id": accession,
                "title": f"{company['company_name']} — SEC {row['form']} filing",
                "publisher": "U.S. Securities and Exchange Commission",
                "published_at": f"{row['filingDate']}T00:00:00+00:00",
                "source_url": url,
                "excerpt": excerpt,
                "categories": categories,
                "relevance_score": relevance,
                "metadata": {"form": row["form"], "accession_number": accession},
            }
        )
    return evidence


def _licensed_news_evidence(company: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.news_api_key:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=settings.news_lookback_days)
    query = (
        f'"{company["company_name"]}" AND '
        "(acquisition OR expansion OR growth OR data center OR investment OR facility)"
    )
    payload = _get_json(
        "https://newsapi.org/v2/everything",
        {"X-Api-Key": settings.news_api_key},
        {
            "q": query[:500],
            "from": cutoff.date().isoformat(),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
        },
    )
    evidence = []
    for article in payload.get("articles", []):
        text = " ".join(
            value for value in (article.get("title"), article.get("description"), article.get("content")) if value
        )
        categories, relevance = classify(text)
        url = article.get("url")
        if not url or not categories:
            continue
        evidence.append(
            {
                "provider": "NEWS_API",
                "external_id": hashlib.sha256(url.encode()).hexdigest(),
                "title": article.get("title") or "Untitled article",
                "publisher": article.get("source", {}).get("name") or "Unknown publisher",
                "published_at": article.get("publishedAt"),
                "source_url": url,
                "excerpt": extract_excerpt(text),
                "categories": categories,
                "relevance_score": relevance,
                "metadata": {"author": article.get("author")},
            }
        )
    return evidence


def _persist(company_id: int, items: list[dict[str, Any]]) -> None:
    with connection() as conn:
        for item in items:
            conn.execute(
                """insert into capacity_planner.news_evidence(
                   news_id,company_id,provider,external_id,title,publisher,published_at,source_url,
                   excerpt,categories,relevance_score,metadata,lookback_days)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(company_id,provider,external_id) do update set
                   title=excluded.title,publisher=excluded.publisher,published_at=excluded.published_at,
                   source_url=excluded.source_url,excerpt=excluded.excerpt,categories=excluded.categories,
                   relevance_score=excluded.relevance_score,metadata=excluded.metadata,
                   lookback_days=excluded.lookback_days,fetched_at=now()""",
                (
                    uuid.uuid4(),
                    company_id,
                    item["provider"],
                    item["external_id"],
                    item["title"],
                    item["publisher"],
                    item["published_at"],
                    item["source_url"],
                    item["excerpt"],
                    item["categories"],
                    item["relevance_score"],
                    json.dumps(item["metadata"]),
                    get_settings().news_lookback_days,
                ),
            )


def _cached(company_id: int) -> list[dict[str, Any]]:
    settings = get_settings()
    with connection() as conn:
        rows = conn.execute(
            """select provider,title,publisher,published_at,source_url,excerpt,categories,
               relevance_score,metadata
               from capacity_planner.news_evidence where company_id=%s
               and fetched_at >= now()-(%s * interval '1 hour')
               and lookback_days=%s
               order by relevance_score desc,published_at desc""",
            (company_id, settings.news_cache_hours, settings.news_lookback_days),
        ).fetchall()
    return [dict(row) for row in rows]


def collect_news(company_id: int) -> dict[str, Any]:
    lookback_days = get_settings().news_lookback_days
    cached = _cached(company_id)
    if cached:
        return {
            "status": "AVAILABLE",
            "cache_hit": True,
            "items": cached,
            "errors": [],
            "providers": {"DATABASE_CACHE": "AVAILABLE"},
            "lookback_days": lookback_days,
        }
    company = _company(company_id)
    items: list[dict[str, Any]] = []
    errors = []
    providers = {}
    for provider_name, provider in (
        ("SEC_EDGAR", _sec_evidence),
        ("NEWS_API", _licensed_news_evidence),
    ):
        if provider_name == "NEWS_API" and not get_settings().news_api_key:
            providers[provider_name] = "NOT_CONFIGURED"
            continue
        try:
            provider_items = provider(company)
            items.extend(provider_items)
            providers[provider_name] = "EVIDENCE_FOUND" if provider_items else "SEARCHED_NO_MATCH"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append({"provider": provider_name, "error": f"{type(exc).__name__}: {exc}"})
            providers[provider_name] = "ERROR"
    deduplicated = {item["source_url"]: item for item in items}
    items = list(deduplicated.values())
    _persist(company_id, items)
    if items:
        status = "DEGRADED" if errors else "AVAILABLE"
    else:
        status = "UNAVAILABLE" if errors else "NO_RELEVANT_EVIDENCE"
    return {
        "status": status,
        "cache_hit": False,
        "items": items,
        "errors": errors,
        "providers": providers,
        "lookback_days": lookback_days,
    }
