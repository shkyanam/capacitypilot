from collections.abc import Callable
from typing import Any

import httpx


def evaluate_api_connectivity(
    base_url: str,
    *,
    timeout_seconds: float = 3,
    requester: Callable[..., Any] = httpx.get,
) -> dict[str, Any]:
    """Evaluate the Streamlit-to-API dependency without relying on the API itself."""
    health_url = f"{str(base_url).rstrip('/')}/health"
    try:
        response = requester(health_url, timeout=timeout_seconds)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": "CONNECTION_REFUSED",
            "message": str(exc),
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }
    except httpx.TimeoutException as exc:
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": "TIMEOUT",
            "message": str(exc),
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }
    except httpx.HTTPStatusError as exc:
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": f"HTTP_{exc.response.status_code}",
            "message": str(exc),
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": "INVALID_OR_UNREACHABLE",
            "message": str(exc),
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }

    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": "INVALID_HEALTH_RESPONSE",
            "message": str(exc),
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {
            "status": "FAIL",
            "check": "API_CONNECTIVITY",
            "failure_type": "UNHEALTHY_RESPONSE",
            "message": "The health endpoint did not return status=ok.",
            "health_url": health_url,
            "recovery_command": "uv run capacity-api",
        }
    return {
        "status": "PASS",
        "check": "API_CONNECTIVITY",
        "failure_type": None,
        "message": "CapacityPilot API is reachable and its PostgreSQL health check passed.",
        "health_url": health_url,
        "recovery_command": None,
    }
