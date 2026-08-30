from typing import Any

from .config import get_settings


def customer_memory_id(company_id: int) -> str:
    return f"capacity-customer-{company_id}"


def _client():
    settings = get_settings()
    if not settings.mem0_enabled:
        return None
    if not settings.mem0_api_key:
        raise RuntimeError("MEM0_ENABLED is true but MEM0_API_KEY is missing")
    from mem0 import MemoryClient

    return MemoryClient(api_key=settings.mem0_api_key)


def search_customer_memory(company_id: int) -> dict[str, Any]:
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - provider failure is intentionally fail-open.
        return _degraded(exc)
    if client is None:
        return {"status": "DISABLED", "items": [], "errors": []}
    settings = get_settings()
    try:
        response = client.search(
            "prior planner decisions, forecast adjustments, and validated expansion outcomes",
            # Hosted Mem0 partitions these records by user_id. Some accounts accept an
            # agent_id on write without indexing it for retrieval, so requiring both fields
            # can hide valid customer memories. The application-specific user prefix keeps
            # this search isolated from other customers and applications.
            filters={"user_id": customer_memory_id(company_id)},
            top_k=settings.mem0_search_top_k,
            threshold=0.1,
        )
        items = response.get("results", []) if isinstance(response, dict) else response
        if not isinstance(items, list):
            items = []
        safe_items = [
            {
                "memory_id": item.get("id"),
                "memory": item.get("memory"),
                "score": item.get("score"),
                "metadata": item.get("metadata", {}),
            }
            for item in items
            if isinstance(item, dict)
        ]
        return {"status": "AVAILABLE", "items": safe_items, "errors": []}
    except Exception as exc:  # noqa: BLE001 - provider failure is intentionally fail-open.
        return _degraded(exc)


def list_application_memories() -> dict[str, Any]:
    """List Mem0 records written by CapacityPilot without exposing other app data."""
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - provider failure is reported to the UI.
        return _degraded(exc)
    if client is None:
        return {"status": "DISABLED", "items": [], "errors": [], "truncated": False}

    page_size = 100
    max_pages = 100
    safe_items: list[dict[str, Any]] = []
    try:
        entities = client.users()
        entity_rows = entities.get("results", []) if isinstance(entities, dict) else []
        customer_partitions = sorted(
            {
                str(row.get("name"))
                for row in entity_rows
                if isinstance(row, dict)
                and row.get("type") == "user"
                and str(row.get("name", "")).startswith("capacity-customer-")
            }
        )
        pages_read = 0
        for user_id in customer_partitions:
            for page in range(1, max_pages + 1):
                pages_read += 1
                response = client.get_all(
                    filters={"user_id": user_id},
                    page=page,
                    page_size=page_size,
                )
                raw_items = response.get("results", []) if isinstance(response, dict) else []
                if not isinstance(raw_items, list):
                    raw_items = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    metadata = item.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    if metadata.get("source") != "capacity_planner_postgres_outbox":
                        continue
                    safe_metadata = {
                        key: metadata.get(key)
                        for key in (
                            "event_type",
                            "company_id",
                            "source",
                            "audit_reference",
                        )
                        if metadata.get(key) is not None
                    }
                    safe_items.append(
                        {
                            "memory_id": item.get("id"),
                            "memory": item.get("memory"),
                            "user_id": item.get("user_id") or user_id,
                            "created_at": item.get("created_at"),
                            "updated_at": item.get("updated_at"),
                            "metadata": safe_metadata,
                        }
                    )
                if len(raw_items) < page_size:
                    break
            else:
                return {
                    "status": "AVAILABLE",
                    "items": safe_items,
                    "errors": [],
                    "truncated": True,
                    "partitions": len(customer_partitions),
                }
        return {
            "status": "AVAILABLE",
            "items": safe_items,
            "errors": [],
            "truncated": False,
            "partitions": len(customer_partitions),
            "pages_read": pages_read,
        }
    except Exception as exc:  # noqa: BLE001 - provider failure is reported to the UI.
        return _degraded(exc)


def _degraded(exc: Exception) -> dict[str, Any]:
    return {
        "status": "DEGRADED",
        "items": [],
        "errors": [{"type": type(exc).__name__, "message": str(exc)[:500]}],
        "truncated": False,
    }


def add_outbox_memory(company_id: int, event_type: str, payload: dict[str, Any]) -> None:
    client = _client()
    if client is None:
        raise RuntimeError("Mem0 is disabled")
    settings = get_settings()
    if event_type == "PLANNER_DECISION":
        content = (
            f"Planner disposition was {payload['decision']} for case {payload['case_id']}. "
            f"Recommendation likelihood band was {payload.get('likelihood_band', 'unknown')} "
            f"with {payload.get('confidence', 'unknown')} confidence."
        )
    else:
        content = (
            f"Validated expansion outcome for case {payload['case_id']}: "
            f"expanded={payload.get('expanded')}."
        )
    client.add(
        messages=[{"role": "user", "content": content}],
        user_id=customer_memory_id(company_id),
        agent_id=settings.mem0_agent_id,
        async_mode=False,
        metadata={
            "event_type": event_type,
            "company_id": company_id,
            "source": "capacity_planner_postgres_outbox",
            "audit_reference": str(payload.get("case_id")),
        },
    )
