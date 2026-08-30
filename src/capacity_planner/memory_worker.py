import logging
import os
import socket
import time

from .config import get_settings
from .db import migrate
from .memory import add_outbox_memory
from .memory_outbox import claim_memory, complete_memory, fail_memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def run_once(worker_id: str) -> bool:
    item = claim_memory(worker_id)
    if not item:
        return False
    try:
        add_outbox_memory(item["company_id"], item["event_type"], item["payload"])
        complete_memory(item["outbox_id"])
        LOG.info("memory_synced outbox_id=%s", item["outbox_id"])
    except Exception as exc:
        fail_memory(item, exc)
        LOG.exception("memory_sync_failed outbox_id=%s", item["outbox_id"])
    return True


def main() -> None:
    migrate()
    settings = get_settings()
    if not settings.mem0_enabled:
        raise RuntimeError("Set MEM0_ENABLED=true before starting the memory worker")
    worker_id = f"memory-{socket.gethostname()}-{os.getpid()}"
    LOG.info("memory_worker_ready worker_id=%s", worker_id)
    try:
        while True:
            if not run_once(worker_id):
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        LOG.info("memory_worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
