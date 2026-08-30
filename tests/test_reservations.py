from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from capacity_planner import api, repository
from capacity_planner.models import LocalReservationRequest


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row


class FakeConnection:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.responder(normalized, params)


def patch_connection(monkeypatch, target, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(target, "connection", factory)


def request_payload(**changes):
    payload = {
        "case_id": "4fe8fcf5-ef59-4fca-aa67-5909d4cca8ba",
        "requested_tib": 135.1,
        "target_date": (datetime.now(UTC).date() + timedelta(days=180)).isoformat(),
        "service": "Storage Capacity",
        "vault_type": "High Performance",
        "region": "us-phoenix-1",
        "qfab": "QFAB-01",
        "planner_identity": "planner@example.com",
        "note": "Capacity reviewed",
        "confirm_local_only": True,
    }
    payload.update(changes)
    return payload


def test_reservation_request_enforces_service_vault_mapping():
    with pytest.raises(ValidationError):
        LocalReservationRequest(**request_payload(vault_type="Archive"))


def test_reservation_request_requires_explicit_local_confirmation():
    with pytest.raises(ValidationError):
        LocalReservationRequest(**request_payload(confirm_local_only=False))


def test_create_local_reservation_is_audited_and_derives_tenancy(monkeypatch):
    created = {
        "reservation_id": "reservation-1",
        "requested_tib": 135.1,
        "status": "LOCAL_RESERVED",
    }

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select cr.company_id,cr.status,cr.recommendation,c.region"):
            return Cursor(
                {
                    "company_id": 7,
                    "status": "REVIEW_REQUIRED",
                    "recommendation": {"likelihood_pct": 90},
                    "region": "us-phoenix-1",
                }
            )
        if sql.startswith("select * from capacity_planner.local_capacity_reservation"):
            return Cursor(None)
        if "from capacity_planner.capacity_inventory" in sql:
            return Cursor(
                {
                    "inventory_id": 10,
                    "inventory_usable": True,
                    "usable_capacity_tib": 1000,
                    "allocated_capacity_tib": 500,
                }
            )
        if sql.startswith("select coalesce(sum(requested_tib),0)"):
            return Cursor({"planning_hold_tib": 0})
        if sql.startswith("insert into capacity_planner.local_capacity_reservation"):
            return Cursor(created)
        if sql.startswith("insert into capacity_planner.planner_decision"):
            return Cursor()
        if sql.startswith("insert into capacity_planner.case_event"):
            return Cursor()
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, repository, fake)
    monkeypatch.setattr(
        repository,
        "get_settings",
        lambda: SimpleNamespace(
            mem0_enabled=False, capacity_inventory_max_age_hours=24
        ),
    )

    result = repository.create_local_reservation(request_payload())

    assert result["created"] is True
    reservation_insert = next(
        call for call in fake.calls if call[0].startswith("insert into capacity_planner.local")
    )
    assert reservation_insert[1][7] == "Shared"
    assert any("planner_decision" in sql for sql, _ in fake.calls)
    assert any("local_capacity_reservation" in sql for sql, _ in fake.calls)


def test_duplicate_reservation_request_returns_existing_record(monkeypatch):
    existing = {"reservation_id": "existing", "status": "LOCAL_RESERVED"}

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select cr.company_id,cr.status,cr.recommendation,c.region"):
            return Cursor(
                {
                    "company_id": 7,
                    "status": "COMPLETE",
                    "recommendation": {},
                    "region": "us-phoenix-1",
                }
            )
        if sql.startswith("select * from capacity_planner.local_capacity_reservation"):
            return Cursor(existing)
        raise AssertionError("Duplicate request must not insert another reservation")

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, repository, fake)
    assert repository.create_local_reservation(request_payload()) == {
        **existing,
        "created": False,
    }


def test_reservation_api_returns_repository_result(monkeypatch):
    expected = {"reservation_id": "reservation-1", "created": True}
    monkeypatch.setattr(api, "create_local_reservation", lambda _payload: expected)
    request = LocalReservationRequest(**request_payload())
    assert api.reserve_locally(request) == {
        **expected,
        "jira_handoffs": [],
        "jira_errors": [],
    }


def test_reservation_approval_queues_cap_and_threshold_hub_jira(monkeypatch):
    case_id = request_payload()["case_id"]
    expected = {
        "reservation_id": "reservation-1",
        "case_id": case_id,
        "created": True,
        "infrastructure_order_recommended": True,
    }
    queued = []
    monkeypatch.setattr(api, "create_local_reservation", lambda _payload: expected)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(jira_enabled=True),
    )
    monkeypatch.setattr(
        api,
        "enqueue_jira_request",
        lambda payload: queued.append(payload) or {"request_type": payload["request_type"]},
    )

    result = api.reserve_locally(LocalReservationRequest(**request_payload()))

    assert [item["request_type"] for item in queued] == [
        "CAP_RESERVATION",
        "HUB_INFRASTRUCTURE",
    ]
    assert [item["request_type"] for item in result["jira_handoffs"]] == [
        "CAP_RESERVATION",
        "HUB_INFRASTRUCTURE",
    ]


def test_reservation_api_converts_invalid_case_to_conflict(monkeypatch):
    def reject(_payload):
        raise LookupError("Completed recommendation not found")

    monkeypatch.setattr(api, "create_local_reservation", reject)
    with pytest.raises(HTTPException) as error:
        api.reserve_locally(LocalReservationRequest(**request_payload()))
    assert error.value.status_code == 409


def test_capacity_availability_subtracts_allocations_and_planning_holds(monkeypatch):
    results = iter(
        [
            Cursor({"company_id": 7, "region": "us-phoenix-1"}),
            Cursor(
                [
                    {
                        "inventory_id": 10,
                        "qfab": "QFAB-A",
                        "usable_capacity_tib": 1000,
                        "allocated_capacity_tib": 500,
                        "planning_hold_tib": 100,
                        "available_capacity_tib": 400,
                        "inventory_usable": True,
                    }
                ]
            ),
        ]
    )
    fake = FakeConnection(lambda _sql, _params: next(results))
    patch_connection(monkeypatch, repository, fake)
    monkeypatch.setattr(
        repository,
        "get_settings",
        lambda: SimpleNamespace(capacity_inventory_max_age_hours=24),
    )

    result = repository.capacity_availability(
        request_payload()["case_id"], "Storage Capacity", "High Performance", 250
    )[0]

    assert float(result["available_after_tib"]) == 150
    assert float(result["post_reservation_allocation_pct"]) == 85
    assert result["capacity_sufficient"] is True
    assert result["infrastructure_order_required"] is True


def test_reservation_rejects_shortfall_before_insert(monkeypatch):
    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select cr.company_id,cr.status,cr.recommendation,c.region"):
            return Cursor(
                {
                    "company_id": 7,
                    "status": "REVIEW_REQUIRED",
                    "recommendation": {"likelihood_pct": 90},
                    "region": "us-phoenix-1",
                }
            )
        if sql.startswith("select * from capacity_planner.local_capacity_reservation"):
            return Cursor(None)
        if "from capacity_planner.capacity_inventory" in sql:
            return Cursor(
                {
                    "inventory_id": 10,
                    "inventory_usable": True,
                    "usable_capacity_tib": 1000,
                    "allocated_capacity_tib": 900,
                }
            )
        if sql.startswith("select coalesce(sum(requested_tib),0)"):
            return Cursor({"planning_hold_tib": 0})
        raise AssertionError("Insufficient capacity must not create a reservation")

    fake = FakeConnection(responder)
    patch_connection(monkeypatch, repository, fake)
    monkeypatch.setattr(
        repository,
        "get_settings",
        lambda: SimpleNamespace(
            mem0_enabled=False, capacity_inventory_max_age_hours=24
        ),
    )

    with pytest.raises(repository.CapacityUnavailableError) as error:
        repository.create_local_reservation(request_payload(requested_tib=135.1))

    assert error.value.details["reason"] == "INSUFFICIENT_CAPACITY"
    assert error.value.details["shortfall_tib"] == pytest.approx(35.1)
    assert not any(
        sql.startswith("insert into capacity_planner.local_capacity_reservation")
        for sql, _ in fake.calls
    )
