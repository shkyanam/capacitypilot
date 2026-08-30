import ast
from pathlib import Path

from capacity_planner import web
from capacity_planner.web import (
    evaluate_link_contract,
    safe_jira_ticket_url,
    safe_source_url,
)


def test_source_link_allows_https():
    assert safe_source_url("https://www.sec.gov/example") == "https://www.sec.gov/example"


def test_source_link_rejects_unsafe_schemes():
    assert safe_source_url("javascript:alert(1)") is None
    assert safe_source_url("http://example.com") is None


def test_jira_link_accepts_matching_stored_url():
    assert safe_jira_ticket_url(
        "CAP-4",
        "https://example.atlassian.net/browse/CAP-4",
        "https://example.atlassian.net",
    ) == "https://example.atlassian.net/browse/CAP-4"


def test_jira_link_rebuilds_missing_or_mismatched_url():
    assert safe_jira_ticket_url(
        "HUB-2", None, "https://example.atlassian.net"
    ) == "https://example.atlassian.net/browse/HUB-2"
    assert safe_jira_ticket_url(
        "HUB-2",
        "https://example.atlassian.net/browse/HUB-99",
        "https://example.atlassian.net",
    ) == "https://example.atlassian.net/browse/HUB-2"


def test_jira_link_rejects_invalid_key_or_unsafe_base():
    assert safe_jira_ticket_url("bad key", None, "https://example.atlassian.net") is None
    assert safe_jira_ticket_url("CAP-1", None, "http://example.atlassian.net") is None


def test_link_contract_eval_passes_all_smoke_checks():
    result = evaluate_link_contract()

    assert result["status"] == "PASS"
    assert result["passed_checks"] == result["total_checks"] == 8
    assert result["failed_checks"] == []


def test_every_web_helper_imported_by_streamlit_ui_exists():
    """Regression check for Streamlit startup ImportError failures."""
    ui_path = Path(__file__).parents[1] / "src" / "capacity_planner" / "ui.py"
    source = ui_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ui_path))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "capacity_planner.web"
        for alias in node.names
    }

    assert imported_names
    assert not [name for name in imported_names if not hasattr(web, name)]
