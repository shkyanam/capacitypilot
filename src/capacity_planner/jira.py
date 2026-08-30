from typing import Any

import httpx

from .config import get_settings


def _adf(lines: list[str]) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": str(line)}],
            }
            for line in lines
        ],
    }


class JiraClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.jira_enabled:
            raise RuntimeError("Set JIRA_ENABLED=true before using Jira integration")
        if not self.settings.jira_base_url:
            raise RuntimeError("JIRA_BASE_URL is required")

    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if self.settings.jira_auth_mode == "basic":
            if not self.settings.jira_user_email or not self.settings.jira_api_token:
                raise RuntimeError("JIRA_USER_EMAIL and JIRA_API_TOKEN are required")
            auth = (self.settings.jira_user_email, self.settings.jira_api_token)
        elif self.settings.jira_auth_mode == "bearer":
            if not self.settings.jira_bearer_token:
                raise RuntimeError("JIRA_BEARER_TOKEN is required")
            headers["Authorization"] = f"Bearer {self.settings.jira_bearer_token}"
        else:
            raise RuntimeError("JIRA_AUTH_MODE must be basic or bearer")
        return httpx.Client(
            base_url=self.settings.jira_base_url.rstrip("/"),
            auth=auth,
            headers=headers,
            timeout=self.settings.jira_timeout_seconds,
            verify=self.settings.jira_verify_ssl,
        )

    def create_or_find_issue(self, request: dict[str, Any]) -> dict[str, Any]:
        idempotency_label = f"capacity-request-{request['jira_request_id']}"
        with self._client() as client:
            search = client.post(
                "/rest/api/3/search/jql",
                json={
                    "jql": (
                        f'project = "{request["project_key"]}" AND '
                        f'labels = "{idempotency_label}"'
                    ),
                    "fields": ["key"],
                    "maxResults": 1,
                },
            )
            search.raise_for_status()
            issues = search.json().get("issues", [])
            if issues:
                issue_key = issues[0]["key"]
                return {
                    "jira_issue_key": issue_key,
                    "jira_issue_url": (
                        f"{self.settings.jira_base_url.rstrip('/')}/browse/{issue_key}"
                    ),
                    "created": False,
                }

            labels = [
                label.strip()
                for label in self.settings.jira_default_labels.split(",")
                if label.strip()
            ]
            labels.append(idempotency_label)
            response = client.post(
                "/rest/api/3/issue",
                json={
                    "fields": {
                        "project": {"key": request["project_key"]},
                        "issuetype": {"name": request["issue_type"]},
                        "summary": request["summary"],
                        "description": _adf(request["payload"]["description_lines"]),
                        "labels": labels,
                    },
                    "properties": [
                        {
                            "key": "capacityPlannerRequestId",
                            "value": str(request["jira_request_id"]),
                        }
                    ],
                },
            )
            response.raise_for_status()
            issue_key = response.json()["key"]
        return {
            "jira_issue_key": issue_key,
            "jira_issue_url": f"{self.settings.jira_base_url.rstrip('/')}/browse/{issue_key}",
            "created": True,
        }
