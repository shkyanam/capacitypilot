from typing import Any

import httpx

from .config import get_settings


def capacity_digest_message(payload: dict[str, Any]) -> dict[str, Any]:
    review = payload["demand_review_count"]
    reserve = payload["reserve_capacity_count"]
    order = payload["order_more_storage_count"]
    planner_url = payload["planner_url"]
    test_mode = bool(payload.get("test_mode"))
    status_mode = bool(payload.get("status_mode"))
    if test_mode:
        title = "Capacity planning alert — SIMULATION"
    elif status_mode:
        title = "Capacity planning status"
    else:
        title = "Capacity planning alert"
    fallback = (
        f"{title}: {review} demand signals waiting for review; "
        f"{reserve} can reserve capacity; {order} need more storage."
    )
    return {
        "text": fallback,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{review}* demand signals are waiting for your review.",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Reserve capacity*\n{reserve}"},
                    {"type": "mrkdwn", "text": f"*Order more storage*\n{order}"},
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open planner review"},
                        "url": planner_url,
                        "action_id": "open_capacity_planner",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ("Planning simulation only. " if test_mode else "")
                            + "Slack is informational only. Capacity decisions require "
                            "planner review in the application."
                        ),
                    }
                ],
            },
        ],
    }


class SlackClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.slack_enabled:
            raise RuntimeError("Set SLACK_ENABLED=true before using Slack integration")

    def send_capacity_digest(self, payload: dict[str, Any]) -> dict[str, str | None]:
        message = capacity_digest_message(payload)
        with httpx.Client(
            timeout=self.settings.slack_timeout_seconds,
            verify=self.settings.slack_verify_ssl,
        ) as client:
            if self.settings.slack_auth_mode == "webhook":
                if not self.settings.slack_webhook_url:
                    raise RuntimeError("SLACK_WEBHOOK_URL is required")
                response = client.post(self.settings.slack_webhook_url, json=message)
                response.raise_for_status()
                if response.text.strip() != "ok":
                    raise RuntimeError(f"Slack webhook rejected the message: {response.text}")
                return {"channel": None, "message_ts": None}
            if self.settings.slack_auth_mode == "bot":
                if not self.settings.slack_bot_token or not self.settings.slack_channel_id:
                    raise RuntimeError(
                        "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID are required"
                    )
                response = client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {self.settings.slack_bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={**message, "channel": self.settings.slack_channel_id},
                )
                response.raise_for_status()
                result = response.json()
                if not result.get("ok"):
                    raise RuntimeError(f"Slack API rejected the message: {result.get('error')}")
                return {"channel": result.get("channel"), "message_ts": result.get("ts")}
        raise RuntimeError("SLACK_AUTH_MODE must be webhook or bot")
