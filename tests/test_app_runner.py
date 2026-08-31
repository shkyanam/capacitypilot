import sys

from capacity_planner.app_runner import service_commands


def test_service_commands_include_all_managed_workers():
    commands = service_commands()

    names = [name for name, _ in commands]

    assert names == [
        "API",
        "capacity worker",
        "News worker",
        "Streamlit",
        "Jira worker",
        "Slack worker",
        "Mem0 worker",
    ]
    assert commands[0][1][:3] == (sys.executable, "-m", "uvicorn")
