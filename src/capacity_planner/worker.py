import logging
import os
import socket
import time

from .agents import AGENT_GRAPH
from .config import get_settings
from .db import event, migrate
from .repository import claim_case, fail_case, finish_case, recover_stale_cases

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def run_once(worker_id: str) -> bool:
    case = claim_case(worker_id)
    if not case:
        return False
    case_id = str(case["case_id"])
    try:
        result = AGENT_GRAPH.invoke(
            {"case_id": case_id, "company_id": case["company_id"], "evidence": {}, "recommendation": {}, "errors": []}
        )
        finish_case(case_id, result["recommendation"])
        LOG.info("case_completed case_id=%s", case_id)
    except Exception as exc:
        event(case_id, "error", {"type": type(exc).__name__, "message": str(exc)})
        fail_case(case, exc)
        LOG.exception("case_failed case_id=%s", case_id)
    return True


def main() -> None:
    migrate()
    recovered = recover_stale_cases()
    if recovered:
        LOG.warning("stale_cases_recovered count=%s", recovered)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    poll_seconds = get_settings().worker_poll_seconds
    LOG.info("worker_ready worker_id=%s poll_seconds=%s", worker_id, poll_seconds)
    try:
        while True:
            if not run_once(worker_id):
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        LOG.info("worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
