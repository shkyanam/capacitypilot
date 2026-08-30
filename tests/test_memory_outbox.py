import json
from contextlib import contextmanager
from types import SimpleNamespace

from capacity_planner import memory_outbox


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = iter(rows or [])
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return Cursor(next(self.rows, None))


def patch_connection(monkeypatch, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(memory_outbox, "connection", factory)


def test_claim_uses_skip_locked_and_marks_running(monkeypatch):
    queued = {"outbox_id": "outbox-1"}
    running = {"outbox_id": "outbox-1", "status": "RUNNING", "attempt_count": 1}
    fake = FakeConnection([queued, running])
    patch_connection(monkeypatch, fake)

    result = memory_outbox.claim_memory("worker-1")

    assert result == running
    assert "skip locked" in fake.calls[0][0].lower()
    assert fake.calls[1][1] == ("worker-1", "outbox-1")


def test_empty_queue_is_idle(monkeypatch):
    fake = FakeConnection([None])
    patch_connection(monkeypatch, fake)
    assert memory_outbox.claim_memory("worker-1") is None


def test_failure_retries_before_attempt_limit(monkeypatch):
    fake = FakeConnection()
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(
        memory_outbox, "get_settings", lambda: SimpleNamespace(mem0_max_attempts=3)
    )
    memory_outbox.fail_memory(
        {"outbox_id": "outbox-1", "attempt_count": 1}, TimeoutError("slow")
    )
    assert fake.calls[0][1][0] == "RETRY"


def test_failure_stops_at_attempt_limit(monkeypatch):
    fake = FakeConnection()
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(
        memory_outbox, "get_settings", lambda: SimpleNamespace(mem0_max_attempts=3)
    )
    memory_outbox.fail_memory(
        {"outbox_id": "outbox-1", "attempt_count": 3}, RuntimeError("bad")
    )
    assert fake.calls[0][1][0] == "FAILED"


def test_enqueue_contains_only_derived_decision_fields():
    fake = FakeConnection()
    memory_outbox.enqueue_planner_decision(
        fake,
        company_id=42,
        case_id="case-1",
        decision="MONITOR",
        recommendation={
            "likelihood_pct": 82,
            "confidence": "MEDIUM",
            "reasons": ["raw customer evidence"],
        },
    )
    payload = json.loads(fake.calls[0][1][2])
    assert payload == {
        "case_id": "case-1",
        "decision": "MONITOR",
        "likelihood_band": "HIGH",
        "confidence": "MEDIUM",
    }
