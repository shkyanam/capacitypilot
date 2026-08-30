import logging
import os
import socket
import time

from .config import get_settings
from .db import migrate
from .slack import SlackClient
from .slack_outbox import (
    claim_slack_alert,
    complete_slack_alert,
    enqueue_capacity_digest,
    fail_slack_alert,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def run_once(worker_id: str) -> bool:
    enqueue_capacity_digest()
    item = claim_slack_alert(worker_id)
    if not item:
        return False
    try:
        result = SlackClient().send_capacity_digest(item["payload"])
        complete_slack_alert(str(item["alert_id"]), result)
        LOG.info("slack_alert_complete alert_id=%s", item["alert_id"])
    except Exception as exc:
        fail_slack_alert(item, exc)
        LOG.exception("slack_alert_failed alert_id=%s", item["alert_id"])
    return True


def main() -> None:
    migrate()
    if not get_settings().slack_enabled:
        raise RuntimeError("Set SLACK_ENABLED=true before starting the Slack worker")
    worker_id = f"slack-{socket.gethostname()}-{os.getpid()}"
    LOG.info("slack_worker_ready worker_id=%s", worker_id)
    try:
        while True:
            if not run_once(worker_id):
                time.sleep(get_settings().worker_poll_seconds)
    except KeyboardInterrupt:
        LOG.info("slack_worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
