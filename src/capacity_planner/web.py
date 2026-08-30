import re
from urllib.parse import urlparse


def safe_source_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def safe_jira_ticket_url(
    issue_key: str | None, issue_url: str | None, jira_base_url: str
) -> str | None:
    """Return a key-matched Jira URL, rebuilding legacy missing URLs when safe."""
    key = str(issue_key or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", key):
        return None
    direct = safe_source_url(str(issue_url or ""))
    if direct and urlparse(direct).path.rstrip("/").endswith(f"/browse/{key}"):
        return direct
    base = safe_source_url(str(jira_base_url or "").rstrip("/"))
    if not base:
        return None
    return f"{base.rstrip('/')}/browse/{key}"


def evaluate_link_contract() -> dict:
    """Run dependency-free smoke checks for links imported by the Streamlit UI."""
    checks = {
        "safe_source_url_exported": callable(safe_source_url),
        "safe_jira_ticket_url_exported": callable(safe_jira_ticket_url),
        "https_source_accepted": (
            safe_source_url("https://www.sec.gov/Archives/example")
            == "https://www.sec.gov/Archives/example"
        ),
        "unsafe_source_rejected": safe_source_url("javascript:alert(1)") is None,
        "matching_jira_url_accepted": (
            safe_jira_ticket_url(
                "CAP-1",
                "https://example.atlassian.net/browse/CAP-1",
                "https://example.atlassian.net",
            )
            == "https://example.atlassian.net/browse/CAP-1"
        ),
        "missing_jira_url_rebuilt": (
            safe_jira_ticket_url(
                "HUB-2", None, "https://example.atlassian.net"
            )
            == "https://example.atlassian.net/browse/HUB-2"
        ),
        "mismatched_jira_url_rebuilt": (
            safe_jira_ticket_url(
                "CAP-3",
                "https://example.atlassian.net/browse/CAP-99",
                "https://example.atlassian.net",
            )
            == "https://example.atlassian.net/browse/CAP-3"
        ),
        "unsafe_jira_base_rejected": (
            safe_jira_ticket_url("CAP-1", None, "http://example.atlassian.net")
            is None
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "passed_checks": len(checks) - len(failed),
        "total_checks": len(checks),
        "failed_checks": failed,
    }
