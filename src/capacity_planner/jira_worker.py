import logging
import os
import socket
import time

from .config import get_settings
from .db import migrate
from .jira import JiraClient
from .jira_outbox import (
    claim_jira_request,
    complete_jira_request,
    fail_jira_request,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def run_once(worker_id: str) -> bool:
    item = claim_jira_request(worker_id)
    if not item:
        return False
    try:
        issue = JiraClient().create_or_find_issue(item)
        complete_jira_request(str(item["jira_request_id"]), issue)
        LOG.info(
            "jira_request_complete request_id=%s issue=%s",
            item["jira_request_id"],
            issue["jira_issue_key"],
        )
    except Exception as exc:
        fail_jira_request(item, exc)
        LOG.exception("jira_request_failed request_id=%s", item["jira_request_id"])
    return True


def main() -> None:
    migrate()
    settings = get_settings()
    worker_id = f"jira-{socket.gethostname()}-{os.getpid()}"
    if not settings.jira_enabled:
        LOG.info("jira_worker_disabled worker_id=%s", worker_id)
        try:
            while True:
                time.sleep(settings.worker_poll_seconds)
        except KeyboardInterrupt:
            LOG.info("jira_worker_stopped worker_id=%s", worker_id)
        return
    LOG.info("jira_worker_ready worker_id=%s", worker_id)
    try:
        while True:
            if not run_once(worker_id):
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        LOG.info("jira_worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
