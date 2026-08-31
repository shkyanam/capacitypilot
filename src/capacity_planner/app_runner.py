"""Run the local CapacityPilot services under one foreground supervisor."""

import signal
import subprocess
import sys
import time
from collections.abc import Sequence

from .config import get_settings
from .db import migrate
from .news_jobs import enqueue_limited


def service_commands() -> list[tuple[str, Sequence[str]]]:
    """Return every process managed by the local application supervisor."""
    commands: list[tuple[str, Sequence[str]]] = [
        ("API", (sys.executable, "-m", "uvicorn", "capacity_planner.api:app", "--host", "0.0.0.0", "--port", "8000")),
        ("capacity worker", (sys.executable, "-m", "capacity_planner.worker")),
        ("News worker", (sys.executable, "-m", "capacity_planner.news_worker")),
        (
            "Streamlit",
            (
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "src/capacity_planner/ui.py",
                "--server.address=0.0.0.0",
            ),
        ),
    ]
    commands.extend(
        [
            ("Jira worker", (sys.executable, "-m", "capacity_planner.jira_worker")),
            ("Slack worker", (sys.executable, "-m", "capacity_planner.slack_worker")),
            ("Mem0 worker", (sys.executable, "-m", "capacity_planner.memory_worker")),
        ]
    )
    return commands


def main() -> None:
    settings = get_settings()
    migrate()
    queued, _ = enqueue_limited(getattr(settings, "news_bulk_company_limit", 100))
    processes = [
        (name, subprocess.Popen(command)) for name, command in service_commands()
    ]
    print("CapacityPilot started: " + ", ".join(name for name, _ in processes))
    print(f"News ingestion queued or refreshed: {queued}")
    print("Open http://localhost:8501. Press Ctrl-C to stop all services.")
    interrupted = False
    try:
        while all(process.poll() is None for _, process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        exited_early = [
            name for name, process in processes if process.poll() not in (None, 0)
        ]
        for _, process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for _, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    if not interrupted and exited_early:
        raise SystemExit(
            "Stopped because these services exited: " + ", ".join(exited_early)
        )


if __name__ == "__main__":
    main()
