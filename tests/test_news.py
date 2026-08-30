from types import SimpleNamespace

from capacity_planner import news
from capacity_planner.news import classify, extract_excerpt


def test_news_classifier_finds_expansion_signals():
    categories, relevance = classify(
        "The company announced an acquisition and a new data center capacity expansion."
    )
    assert "acquisition" in categories
    assert "data_center" in categories
    assert relevance >= 0.6


def test_excerpt_is_bounded_and_signal_focused():
    text = "background " * 200 + "capacity expansion announced " + "details " * 200
    excerpt = extract_excerpt(text)
    assert len(excerpt) <= 700
    assert "capacity expansion" in excerpt


def test_successful_search_without_matches_is_not_provider_unavailable(monkeypatch):
    monkeypatch.setattr(news, "_cached", lambda _company_id: [])
    monkeypatch.setattr(news, "_company", lambda _company_id: {"company_name": "Example"})
    monkeypatch.setattr(news, "_sec_evidence", lambda _company: [])
    monkeypatch.setattr(news, "_persist", lambda *_args: None)
    monkeypatch.setattr(
        news,
        "get_settings",
        lambda: SimpleNamespace(news_api_key="", news_lookback_days=180),
    )
    result = news.collect_news(1)
    assert result["status"] == "NO_RELEVANT_EVIDENCE"
    assert result["providers"] == {
        "SEC_EDGAR": "SEARCHED_NO_MATCH",
        "NEWS_API": "NOT_CONFIGURED",
    }
    assert result["errors"] == []
    assert result["lookback_days"] == 180
