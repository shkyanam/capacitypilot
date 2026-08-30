"""Run the local CapacityPilot services under one foreground supervisor."""

import signal
import subprocess
import sys
import time
from collections.abc import Sequence

from .config import Settings, get_settings


def service_commands(settings: Settings | None = None) -> list[tuple[str, Sequence[str]]]:
    """Return the services required by the current local configuration."""
    settings = settings or get_settings()
    commands: list[tuple[str, Sequence[str]]] = [
        ("API", (sys.executable, "-m", "uvicorn", "capacity_planner.api:app", "--host", "0.0.0.0", "--port", "8000")),
        ("capacity worker", (sys.executable, "-m", "capacity_planner.worker")),
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
    if settings.jira_enabled:
        commands.append(("Jira worker", (sys.executable, "-m", "capacity_planner.jira_worker")))
    if settings.slack_enabled:
        commands.append(("Slack worker", (sys.executable, "-m", "capacity_planner.slack_worker")))
    if settings.mem0_enabled:
        commands.append(("Mem0 worker", (sys.executable, "-m", "capacity_planner.memory_worker")))
    return commands


def main() -> None:
    processes = [
        (name, subprocess.Popen(command)) for name, command in service_commands()
    ]
    print("CapacityPilot started: " + ", ".join(name for name, _ in processes))
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
