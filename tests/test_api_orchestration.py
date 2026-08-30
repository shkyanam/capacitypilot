from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from capacity_planner import api
from capacity_planner.models import BulkForecastOverrideRequest, DecisionRequest


@pytest.fixture(autouse=True)
def default_api_settings(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(mem0_enabled=False, api_auth_token="test-token"),
    )


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row


class DecisionConnection:
    def __init__(self, status):
        self.status = status
        self.inserted = False

    def execute(self, sql, _params=()):
        if sql.strip().startswith("select status"):
            return Cursor(
                {
                    "status": self.status,
                    "company_id": 7,
                    "recommendation": {"likelihood_pct": 80, "confidence": "MEDIUM"},
                }
                if self.status
                else None
            )
        if sql.strip().startswith("insert into capacity_planner.planner_decision"):
            self.inserted = True
            return Cursor({"decision_id": 1, "decision": "APPROVE_REVIEW"})
        raise AssertionError(sql)


def patch_connection(monkeypatch, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(api, "connection", factory)


def decision_request():
    return DecisionRequest(
        decision="APPROVE_REVIEW",
        note="Evidence reviewed",
        decided_by="planner@example.com",
    )


def test_unfinished_case_cannot_receive_human_approval(monkeypatch):
    fake = DecisionConnection("RUNNING")
    patch_connection(monkeypatch, fake)
    with pytest.raises(HTTPException) as error:
        api.decide("case-1", decision_request())
    assert error.value.status_code == 409
    assert fake.inserted is False


def test_review_ready_case_accepts_human_decision(monkeypatch):
    fake = DecisionConnection("REVIEW_REQUIRED")
    patch_connection(monkeypatch, fake)
    result = api.decide("case-1", decision_request())
    assert result["decision"] == "APPROVE_REVIEW"
    assert fake.inserted is True


def test_enabled_memory_queues_decision_after_postgres_write(monkeypatch):
    fake = DecisionConnection("REVIEW_REQUIRED")
    patch_connection(monkeypatch, fake)
    queued = []
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(mem0_enabled=True))
    monkeypatch.setattr(api, "enqueue_planner_decision", lambda conn, **values: queued.append(values))

    api.decide("case-1", decision_request())

    assert queued == [
        {
            "company_id": 7,
            "case_id": "case-1",
            "decision": "APPROVE_REVIEW",
            "recommendation": {"likelihood_pct": 80, "confidence": "MEDIUM"},
        }
    ]


def test_api_authentication_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(api_auth_token="secret"))
    with pytest.raises(HTTPException) as error:
        api.authorize("wrong")
    assert error.value.status_code == 401


def test_api_authentication_accepts_matching_token(monkeypatch):
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(api_auth_token="secret"))
    assert api.authorize("secret") is None


def test_investigation_returns_created_case(monkeypatch):
    monkeypatch.setattr(api, "create_case", lambda company_id: {"company_id": company_id, "status": "QUEUED"})
    result = api.investigate(api.InvestigationRequest(company_id=9))
    assert result == {"company_id": 9, "status": "QUEUED"}


def test_missing_company_is_returned_as_404(monkeypatch):
    def missing(_company_id):
        raise LookupError("Company not found")

    monkeypatch.setattr(api, "create_case", missing)
    with pytest.raises(HTTPException) as error:
        api.investigate(api.InvestigationRequest(company_id=9))
    assert error.value.status_code == 404


class CaseConnection:
    def __init__(self, case_row, events=None):
        self.case_row = case_row
        self.events = events or []
        self.calls = 0

    def execute(self, _sql, _params=()):
        self.calls += 1
        return Cursor(self.case_row if self.calls == 1 else self.events)


def test_case_endpoint_includes_event_trail(monkeypatch):
    fake = CaseConnection({"case_id": "case-1", "status": "COMPLETE"}, [{"event_type": "news"}])
    patch_connection(monkeypatch, fake)
    result = api.case("case-1")
    assert result["status"] == "COMPLETE"
    assert result["events"] == [{"event_type": "news"}]


def test_case_endpoint_returns_404_for_unknown_case(monkeypatch):
    fake = CaseConnection(None)
    patch_connection(monkeypatch, fake)
    with pytest.raises(HTTPException) as error:
        api.case("missing")
    assert error.value.status_code == 404


def test_memories_endpoint_enriches_provider_records_with_customer(monkeypatch):
    monkeypatch.setattr(
        api,
        "list_application_memories",
        lambda: {
            "status": "AVAILABLE",
            "items": [
                {
                    "memory_id": "memory-1",
                    "memory": "Planner disposition was MONITOR.",
                    "metadata": {"company_id": 7, "event_type": "PLANNER_DECISION"},
                }
            ],
            "errors": [],
            "truncated": False,
        },
    )
    patch_connection(
        monkeypatch,
        ListConnection([{"company_id": 7, "company_name": "Example Co", "ticker": "EX"}]),
    )

    result = api.memories()

    assert result["count"] == 1
    assert result["items"][0]["company_name"] == "Example Co"
    assert result["items"][0]["ticker"] == "EX"


class ListConnection:
    def __init__(self, rows):
        self.rows = rows
        self.params = None
        self.sql = None

    def execute(self, sql, params=()):
        self.sql = " ".join(sql.split())
        self.params = params
        return Cursor(self.rows)


def test_shortlist_returns_ranked_latest_recommendations(monkeypatch):
    rows = [{"company_name": "Example", "likelihood_pct": 90}]
    fake = ListConnection(rows)
    patch_connection(monkeypatch, fake)
    assert api.shortlist(80) == rows
    assert fake.params == (80,)


def test_shortlist_threshold_is_bounded(monkeypatch):
    fake = ListConnection([])
    patch_connection(monkeypatch, fake)
    api.shortlist(500)
    assert fake.params == (100,)


def test_shortlist_can_return_only_unresolved_planner_items(monkeypatch):
    fake = ListConnection([])
    patch_connection(monkeypatch, fake)
    api.shortlist(75, pending_only=True)
    assert "planner_decision" in fake.sql
    assert "local_capacity_reservation" in fake.sql


def test_portfolio_enqueue_returns_progress(monkeypatch):
    monkeypatch.setattr(api, "enqueue_initial_portfolio", lambda: 996)
    monkeypatch.setattr(
        api,
        "portfolio_status",
        lambda: {"total_companies": 1000, "scored_companies": 4},
    )
    assert api.enqueue_portfolio_investigation() == {
        "queued": 996,
        "total_companies": 1000,
        "scored_companies": 4,
    }


def test_bulk_forecast_override_saves_multiple_planner_changes(monkeypatch):
    captured = []
    monkeypatch.setattr(
        api,
        "save_forecast_overrides",
        lambda values, **metadata: captured.append((values, metadata)) or len(values),
    )
    request = BulkForecastOverrideRequest(
        overrides=[
            {
                "case_id": "4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba",
                "likelihood_pct": 75,
                "confidence": "MEDIUM",
                "timing_days": 90,
                "capacity_growth_tib": 250,
                "action": "PLANNER_REVIEW",
            }
        ],
        modified_by="planner@example.com",
        note="Customer confirmed timing",
    )

    assert api.bulk_forecast_override(request) == {"updated": 1}
    assert captured[0][1] == {
        "modified_by": "planner@example.com",
        "note": "Customer confirmed timing",
    }


def test_bulk_forecast_override_rejects_duplicate_case_ids():
    item = {
        "case_id": "4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba",
        "likelihood_pct": 75,
        "confidence": "MEDIUM",
        "action": "MONITOR",
    }
    request = BulkForecastOverrideRequest(
        overrides=[item, item], modified_by="planner@example.com"
    )
    with pytest.raises(HTTPException) as error:
        api.bulk_forecast_override(request)
    assert error.value.status_code == 422
