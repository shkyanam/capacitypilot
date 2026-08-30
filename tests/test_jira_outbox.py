from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from capacity_planner import jira_outbox


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.responder(normalized, params)


def patch_connection(monkeypatch, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(jira_outbox, "connection", factory)
    monkeypatch.setattr(
        jira_outbox,
        "get_settings",
        lambda: SimpleNamespace(
            jira_enabled=True,
            jira_capacity_project_key="CAP",
            jira_capacity_issue_type="Task",
            jira_hub_project_key="HUB",
            jira_hub_issue_type="Task",
        ),
    )


def case():
    return {
        "company_id": 7,
        "company_name": "Example Co",
        "ticker": "EX",
        "region": "us-phoenix-1",
        "recommendation": {"likelihood_pct": 90},
    }


def test_cap_request_requires_inventory_backed_reservation(monkeypatch):
    responses = iter(
        [
            Cursor(case()),
            Cursor(None),
            Cursor({"inventory_id": None}),
        ]
    )
    patch_connection(monkeypatch, FakeConnection(lambda _sql, _params: next(responses)))

    with pytest.raises(LookupError, match="inventory-backed"):
        jira_outbox.enqueue_jira_request(
            {
                "case_id": "case-1",
                "request_type": "CAP_RESERVATION",
                "planner_identity": "planner@example.com",
            }
        )


def test_cap_request_routes_reserved_capacity_to_cap(monkeypatch):
    reservation = {
        "reservation_id": "reservation-1",
        "inventory_id": 10,
        "requested_tib": 100,
        "region": "us-phoenix-1",
        "qfab": "QFAB-01",
        "service": "Storage Capacity",
        "vault_type": "High Performance",
        "tenancy_type": "Shared",
        "available_before_tib": 400,
        "available_after_tib": 300,
        "target_date": "2026-12-01",
    }

    def responder(sql, _params):
        if "from capacity_planner.case_run" in sql:
            return Cursor(case())
        if "from capacity_planner.jira_request" in sql:
            return Cursor(None)
        if "from capacity_planner.local_capacity_reservation" in sql:
            return Cursor(reservation)
        if sql.startswith("insert into capacity_planner.jira_request"):
            return Cursor({"project_key": "CAP", "request_type": "CAP_RESERVATION"})
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, fake)
    result = jira_outbox.enqueue_jira_request(
        {
            "case_id": "case-1",
            "request_type": "CAP_RESERVATION",
            "planner_identity": "planner@example.com",
            "note": "approved",
        }
    )

    assert result["project_key"] == "CAP"
    insert = next(call for call in fake.calls if call[0].startswith("insert into"))
    assert insert[1][5] == "CAP_RESERVATION"


def test_shortage_request_routes_to_hub_with_order_size(monkeypatch):
    option = {
        "inventory_id": 10,
        "qfab": "QFAB-01",
        "service": "Storage Capacity",
        "vault_type": "High Performance",
        "tenancy_type": "Shared",
        "requested_tib": 500,
        "available_capacity_tib": 100,
        "shortfall_tib": 400,
        "usable_capacity_tib": 1000,
        "allocated_capacity_tib": 900,
        "planning_hold_tib": 0,
        "capacity_sufficient": False,
    }

    def responder(sql, _params):
        if "from capacity_planner.case_run" in sql:
            return Cursor(case())
        if "from capacity_planner.jira_request" in sql:
            return Cursor(None)
        if "from capacity_planner.local_capacity_reservation" in sql:
            return Cursor(None)
        if sql.startswith("insert into capacity_planner.jira_request"):
            return Cursor({"project_key": "HUB", "request_type": "HUB_INFRASTRUCTURE"})
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(jira_outbox, "capacity_availability", lambda *_args: [option])
    result = jira_outbox.enqueue_jira_request(
        {
            "case_id": "case-1",
            "request_type": "HUB_INFRASTRUCTURE",
            "service": "Storage Capacity",
            "vault_type": "High Performance",
            "qfab": "QFAB-01",
            "requested_tib": 500,
            "target_date": "2026-12-01",
            "planner_identity": "planner@example.com",
        }
    )

    assert result["project_key"] == "HUB"
    insert = next(call for call in fake.calls if call[0].startswith("insert into"))
    assert "Order 1,000.0 TiB infrastructure" in insert[1][8]


def test_regional_hub_request_has_no_customer_and_uses_verified_pool(monkeypatch):
    inventory = {
        "inventory_id": 10,
        "inventory_usable": True,
        "usable_capacity_tib": 1000,
        "allocated_capacity_tib": 800,
        "planning_hold_tib": 50,
        "available_capacity_tib": 150,
    }

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if "from capacity_planner.capacity_inventory" in sql:
            return Cursor(inventory)
        if "from capacity_planner.jira_request" in sql:
            return Cursor(None)
        if sql.startswith("insert into capacity_planner.jira_request"):
            return Cursor(
                {
                    "jira_request_id": "regional-1",
                    "case_id": None,
                    "company_id": None,
                    "project_key": "HUB",
                    "request_type": "HUB_INFRASTRUCTURE",
                }
            )
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(
        jira_outbox,
        "get_settings",
        lambda: SimpleNamespace(
            jira_enabled=True,
            jira_hub_project_key="HUB",
            jira_hub_issue_type="Task",
            capacity_inventory_max_age_hours=24,
        ),
    )

    result = jira_outbox.enqueue_jira_request(
        {
            "case_id": None,
            "request_type": "HUB_INFRASTRUCTURE",
            "region": "us-phoenix-1",
            "service": "Storage Capacity",
            "vault_type": "High Performance",
            "qfab": "QFAB-01",
            "requested_tib": 500,
            "target_date": "2026-12-01",
            "planner_identity": "planner@example.com",
            "note": "Restore regional headroom",
        }
    )

    assert result["case_id"] is None
    assert result["company_id"] is None
    insert = next(call for call in fake.calls if call[0].startswith("insert into"))
    assert "Order 500.0 TiB infrastructure" in insert[1][4]
    assert insert[1][6] == "planner@example.com"
