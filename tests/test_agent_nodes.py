from datetime import UTC, datetime
from decimal import Decimal

from capacity_planner import agents


def production_signal():
    return {
        "company_name": "Example Corp",
        "sec_cik": 123,
        "ticker": "EXM",
        "installed_tib": Decimal(100),
        "consumed_tib": Decimal(80),
        "trailing_12m_growth_tib": Decimal(20),
        "prior_expansion_count": 2,
        "avg_prior_expansion_tib": Decimal(25),
        "open_demand_tib": Decimal(10),
        "demand_stage": "QUALIFIED",
        "source_freshness": "FRESH",
        "data_classification": "PRODUCTION",
        "generated_at": datetime.now(UTC),
        "duplicate_cik_count": 1,
        "duplicate_ticker_exchange_count": 1,
    }


def state(evidence=None):
    return {
        "case_id": "case-1",
        "company_id": 1,
        "evidence": evidence or {},
        "recommendation": {},
        "errors": [],
    }


def test_data_quality_node_persists_scored_evidence(monkeypatch):
    events = []
    monkeypatch.setattr(agents, "_one", lambda *_args: production_signal())
    monkeypatch.setattr(agents, "event", lambda *args: events.append(args))
    result = agents.data_quality(state())
    assert result["evidence"]["data_quality"]["quality_score_pct"] == 100
    assert events[0][1] == "data_quality"


def test_storage_demand_and_news_nodes_preserve_prior_evidence(monkeypatch):
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    monkeypatch.setattr(agents, "_one", lambda *_args: {"value": 1})
    start = state({"data_quality": {"passed": True}})
    storage = agents.storage_history(start)
    assert "data_quality" in storage["evidence"]
    demand = agents.demand({**start, "evidence": storage["evidence"]})
    assert demand["evidence"]["demand"] == {"value": 1}
    monkeypatch.setattr(
        agents,
        "collect_news",
        lambda _company_id: {"status": "AVAILABLE", "items": [], "errors": []},
    )
    news = agents.news({**start, "evidence": demand["evidence"]})
    assert news["evidence"]["news"]["status"] == "AVAILABLE"


def test_evaluation_reports_missing_specialists(monkeypatch):
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.evaluation(state({"data_quality": {}}))["evidence"]["evaluation"]
    assert result["evidence_coverage_pct"] == 25
    assert result["missing_specialists"] == ["demand", "news", "storage_history"]
