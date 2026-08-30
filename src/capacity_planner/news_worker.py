import logging
import os
import socket
import time

from .config import get_settings
from .db import migrate
from .news import collect_news
from .news_jobs import claim_job, fail_job, finish_job, recover_stale_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def validate_sec_user_agent(value: str) -> None:
    lowered = value.lower()
    if "@" not in value or "example.com" in lowered or "admin@example" in lowered:
        raise RuntimeError(
            "SEC_USER_AGENT must identify your organization and a monitored contact email"
        )


def run_once(worker_id: str) -> bool:
    job = claim_job(worker_id)
    if not job:
        return False
    company_id = job["company_id"]
    try:
        result = collect_news(company_id)
        finish_job(job, result)
        LOG.info(
            "news_job_completed company_id=%s status=%s evidence=%s",
            company_id,
            result["status"],
            len(result.get("items", [])),
        )
    except Exception as exc:
        fail_job(job, exc)
        LOG.exception("news_job_failed company_id=%s", company_id)
    return True


def main() -> None:
    migrate()
    recovered = recover_stale_jobs()
    if recovered:
        LOG.warning("stale_news_jobs_recovered count=%s", recovered)
    worker_id = f"news-{socket.gethostname()}-{os.getpid()}"
    settings = get_settings()
    validate_sec_user_agent(settings.sec_user_agent)
    LOG.info(
        "news_worker_ready worker_id=%s company_interval_seconds=%s",
        worker_id,
        settings.news_bulk_company_interval_seconds,
    )
    try:
        while True:
            processed = run_once(worker_id)
            time.sleep(
                settings.news_bulk_company_interval_seconds
                if processed
                else settings.worker_poll_seconds
            )
    except KeyboardInterrupt:
        LOG.info("news_worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
