from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from capacity_planner import slack_outbox, slack_worker


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row


class FakeConnection:
    def __init__(self, responses):
        self.responses = iter(responses)

    def execute(self, _sql, _params=()):
        return next(self.responses)


def patch_connection(monkeypatch, responses):
    @contextmanager
    def factory():
        yield FakeConnection(responses)

    monkeypatch.setattr(slack_outbox, "connection", factory)


def settings(**changes):
    values = {
        "capacity_inventory_max_age_hours": 24,
        "slack_require_production_eligible": True,
        "slack_include_test_scenarios": False,
        "slack_planner_url": "https://planner.example.com",
        "slack_enabled": True,
        "slack_digest_interval_minutes": 15,
        "slack_max_attempts": 5,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def eligible_row(*, available, growth=100, scenario_id=None):
    return {
        "case_id": "case-1",
        "company_id": 1,
        "company_name": "Example Co",
        "ticker": "EX",
        "region": "us-phoenix-1",
        "scenario_id": scenario_id,
        "growth_tib": Decimal(growth),
        "regional_available_tib": Decimal(available),
        "updated_at": datetime.now(UTC),
    }


def test_summary_partitions_reserve_and_order_actions(monkeypatch):
    patch_connection(
        monkeypatch,
        [Cursor([eligible_row(available=200), eligible_row(available=50)])],
    )
    monkeypatch.setattr(slack_outbox, "get_settings", settings)
    result = slack_outbox.capacity_alert_summary()
    assert result["demand_review_count"] == 2
    assert result["reserve_capacity_count"] == 1
    assert result["order_more_storage_count"] == 1


def test_summary_labels_included_test_scenarios(monkeypatch):
    patch_connection(
        monkeypatch,
        [Cursor([eligible_row(available=200, scenario_id="scenario-1")])],
    )
    monkeypatch.setattr(
        slack_outbox,
        "get_settings",
        lambda: settings(slack_include_test_scenarios=True),
    )

    result = slack_outbox.capacity_alert_summary()

    assert result["test_scenario_count"] == 1
    assert result["test_mode"] is True


def test_empty_summary_does_not_queue_alert(monkeypatch):
    monkeypatch.setattr(
        slack_outbox,
        "capacity_alert_summary",
        lambda: {"demand_review_count": 0},
    )
    monkeypatch.setattr(slack_outbox, "get_settings", settings)
    assert slack_outbox.enqueue_capacity_digest() is None


def test_worker_delivers_claimed_alert(monkeypatch):
    item = {"alert_id": "alert-1", "payload": {}}
    completed = []

    class FakeSlackClient:
        def send_capacity_digest(self, _payload):
            return {"channel": "C123", "message_ts": "123.4"}

    monkeypatch.setattr(slack_worker, "enqueue_capacity_digest", lambda: None)
    monkeypatch.setattr(slack_worker, "claim_slack_alert", lambda _worker: item)
    monkeypatch.setattr(slack_worker, "SlackClient", FakeSlackClient)
    monkeypatch.setattr(
        slack_worker,
        "complete_slack_alert",
        lambda alert_id, result: completed.append((alert_id, result)),
    )
    assert slack_worker.run_once("worker-1") is True
    assert completed == [("alert-1", {"channel": "C123", "message_ts": "123.4"})]
