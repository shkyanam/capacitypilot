import sys
from types import SimpleNamespace

from capacity_planner.app_runner import service_commands


def test_service_commands_include_core_and_enabled_workers():
    commands = service_commands(
        SimpleNamespace(jira_enabled=True, slack_enabled=False, mem0_enabled=True)
    )

    names = [name for name, _ in commands]

    assert names == ["API", "capacity worker", "Streamlit", "Jira worker", "Mem0 worker"]
    assert commands[0][1][:3] == (sys.executable, "-m", "uvicorn")
