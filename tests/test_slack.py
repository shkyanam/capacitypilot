from types import SimpleNamespace

import httpx
import pytest

from capacity_planner import slack


def settings(**changes):
    values = {
        "slack_enabled": True,
        "slack_auth_mode": "webhook",
        "slack_webhook_url": "https://hooks.slack.test/services/test",
        "slack_bot_token": "xoxb-test",
        "slack_channel_id": "C123",
        "slack_timeout_seconds": 10,
        "slack_verify_ssl": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def payload():
    return {
        "demand_review_count": 5,
        "reserve_capacity_count": 2,
        "order_more_storage_count": 3,
        "planner_url": "https://planner.example.com",
    }


def patch_client(monkeypatch, handler, **setting_changes):
    original = httpx.Client
    monkeypatch.setattr(slack, "get_settings", lambda: settings(**setting_changes))
    monkeypatch.setattr(
        slack.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_capacity_digest_contains_requested_counts_and_planner_link():
    message = slack.capacity_digest_message(payload())
    assert "5 demand signals" in message["text"]
    assert message["blocks"][2]["fields"][0]["text"] == "*Reserve capacity*\n2"
    assert message["blocks"][2]["fields"][1]["text"] == "*Order more storage*\n3"
    assert message["blocks"][3]["elements"][0]["url"] == "https://planner.example.com"


def test_capacity_digest_marks_test_notifications():
    message = slack.capacity_digest_message(payload() | {"test_mode": True})
    assert "— SIMULATION" in message["text"]
    assert "Planning simulation only" in message["blocks"][4]["elements"][0]["text"]


def test_capacity_digest_can_render_truthful_live_status():
    message = slack.capacity_digest_message(payload() | {"status_mode": True})
    assert message["blocks"][0]["text"]["text"] == "Capacity planning status"
    assert "SIMULATION" not in message["text"]




def test_webhook_sends_block_message(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="ok")

    patch_client(monkeypatch, handler)
    result = slack.SlackClient().send_capacity_digest(payload())
    assert result == {"channel": None, "message_ts": None}
    assert requests[0].url.host == "hooks.slack.test"
    assert b"Open planner review" in requests[0].read()


def test_bot_mode_checks_slack_response(monkeypatch):
    def handler(_request):
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    patch_client(monkeypatch, handler, slack_auth_mode="bot")
    with pytest.raises(RuntimeError, match="channel_not_found"):
        slack.SlackClient().send_capacity_digest(payload())
