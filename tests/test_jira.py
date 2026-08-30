from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from capacity_planner import jira, jira_worker
from capacity_planner.models import JiraRequestCreate


def settings():
    return SimpleNamespace(
        jira_enabled=True,
        jira_base_url="https://example.atlassian.net",
        jira_auth_mode="basic",
        jira_user_email="planner@example.com",
        jira_api_token="secret",
        jira_bearer_token="",
        jira_timeout_seconds=10,
        jira_verify_ssl=True,
        jira_default_labels="capacity-planner,storage-expansion",
    )


def client_with_transport(monkeypatch, handler):
    monkeypatch.setattr(jira, "get_settings", settings)
    client = jira.JiraClient()

    @contextmanager
    def factory():
        with httpx.Client(
            base_url=settings().jira_base_url,
            transport=httpx.MockTransport(handler),
        ) as http_client:
            yield http_client

    monkeypatch.setattr(client, "_client", factory)
    return client


def request():
    return {
        "jira_request_id": "4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba",
        "project_key": "CAP",
        "issue_type": "Task",
        "summary": "Reserve 100 TiB",
        "payload": {"description_lines": ["Customer: Example", "Region: PHX"]},
    }


def test_jira_client_reuses_issue_found_by_idempotency_label(monkeypatch):
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, json={"issues": [{"key": "CAP-17"}]})

    result = client_with_transport(monkeypatch, handler).create_or_find_issue(request())

    assert result["jira_issue_key"] == "CAP-17"
    assert result["created"] is False
    assert len(calls) == 1
    assert calls[0].url.path == "/rest/api/3/search/jql"


def test_jira_client_creates_adf_issue_with_idempotency_label(monkeypatch):
    bodies = []

    def handler(http_request):
        bodies.append(http_request.read().decode())
        if http_request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(201, json={"key": "HUB-8"})

    payload = request() | {"project_key": "HUB"}
    result = client_with_transport(monkeypatch, handler).create_or_find_issue(payload)

    assert result["jira_issue_key"] == "HUB-8"
    assert result["created"] is True
    assert '"type":"doc"' in bodies[1]
    assert "capacity-request-4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba" in bodies[1]


def test_jira_request_requires_explicit_confirmation():
    with pytest.raises(ValidationError):
        JiraRequestCreate(
            case_id="4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba",
            request_type="CAP_RESERVATION",
            planner_identity="planner@example.com",
            confirm_create=False,
        )


def test_regional_hub_request_does_not_require_customer_case():
    request = JiraRequestCreate(
        request_type="HUB_INFRASTRUCTURE",
        region="us-phoenix-1",
        service="Storage Capacity",
        vault_type="High Performance",
        qfab="QFAB-01",
        requested_tib=500,
        target_date=datetime.now(UTC).date() + timedelta(days=90),
        planner_identity="planner@example.com",
        confirm_create=True,
    )

    assert request.case_id is None


def test_cap_request_still_requires_customer_case():
    with pytest.raises(ValidationError, match="case_id is required"):
        JiraRequestCreate(
            request_type="CAP_RESERVATION",
            planner_identity="planner@example.com",
            confirm_create=True,
        )


def test_jira_worker_completes_claimed_request(monkeypatch):
    item = {"jira_request_id": "request-1"}
    completed = []
    monkeypatch.setattr(jira_worker, "claim_jira_request", lambda _worker: item)
    monkeypatch.setattr(
        jira_worker.JiraClient,
        "create_or_find_issue",
        lambda _self, _item: {"jira_issue_key": "CAP-1", "jira_issue_url": "url"},
    )
    monkeypatch.setattr(
        jira_worker, "complete_jira_request", lambda request_id, issue: completed.append((request_id, issue))
    )

    assert jira_worker.run_once("worker-1") is True
    assert completed[0][0] == "request-1"


def test_jira_worker_retries_failed_request(monkeypatch):
    item = {"jira_request_id": "request-1"}
    failures = []
    monkeypatch.setattr(jira_worker, "claim_jira_request", lambda _worker: item)
    monkeypatch.setattr(
        jira_worker.JiraClient,
        "create_or_find_issue",
        lambda _self, _item: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
    )
    monkeypatch.setattr(
        jira_worker, "fail_jira_request", lambda claimed, error: failures.append((claimed, error))
    )

    assert jira_worker.run_once("worker-1") is True
    assert isinstance(failures[0][1], httpx.TimeoutException)
