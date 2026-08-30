from types import SimpleNamespace

from capacity_planner import memory


class FakeClient:
    def __init__(
        self, *, search_result=None, get_all_results=None, users_result=None, error=None
    ):
        self.search_result = search_result
        self.get_all_results = get_all_results or {}
        self.users_result = users_result or {"results": []}
        self.error = error
        self.search_call = None
        self.add_call = None
        self.get_all_calls = []

    def search(self, query, **kwargs):
        self.search_call = (query, kwargs)
        if self.error:
            raise self.error
        return self.search_result

    def add(self, **kwargs):
        self.add_call = kwargs

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.get_all_results.get(kwargs["page"], {"results": []})

    def users(self):
        if self.error:
            raise self.error
        return self.users_result


def settings(**overrides):
    values = {
        "mem0_enabled": True,
        "mem0_api_key": "test-key",
        "mem0_agent_id": "capacity-planner",
        "mem0_search_top_k": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_customer_memory_is_partitioned_by_company():
    assert memory.customer_memory_id(42) == "capacity-customer-42"


def test_disabled_memory_is_neutral(monkeypatch):
    monkeypatch.setattr(memory, "get_settings", lambda: settings(mem0_enabled=False))
    assert memory.search_customer_memory(42) == {
        "status": "DISABLED",
        "items": [],
        "errors": [],
    }


def test_search_returns_only_allowlisted_fields(monkeypatch):
    client = FakeClient(
        search_result={
            "results": [
                {
                    "id": "memory-1",
                    "memory": "Planner disposition was MONITOR.",
                    "score": 0.91,
                    "metadata": {"event_type": "PLANNER_DECISION"},
                    "provider_internal": "must not leak",
                }
            ]
        }
    )
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.setattr(memory, "get_settings", settings)

    result = memory.search_customer_memory(42)

    assert result["status"] == "AVAILABLE"
    assert result["items"] == [
        {
            "memory_id": "memory-1",
            "memory": "Planner disposition was MONITOR.",
            "score": 0.91,
            "metadata": {"event_type": "PLANNER_DECISION"},
        }
    ]
    assert client.search_call[1]["filters"] == {"user_id": "capacity-customer-42"}


def test_provider_failure_degrades_memory_without_raising(monkeypatch):
    monkeypatch.setattr(memory, "_client", lambda: FakeClient(error=TimeoutError("slow")))
    monkeypatch.setattr(memory, "get_settings", settings)
    result = memory.search_customer_memory(42)
    assert result["status"] == "DEGRADED"
    assert result["items"] == []
    assert result["errors"][0]["type"] == "TimeoutError"


def test_list_application_memories_returns_only_capacitypilot_records(monkeypatch):
    client = FakeClient(
        users_result={
            "results": [
                {"name": "capacity-customer-42", "type": "user"},
                {"name": "someone-else", "type": "user"},
                {"name": "capacity-planner", "type": "agent"},
            ]
        },
        get_all_results={
            1: {
                "results": [
                    {
                        "id": "memory-1",
                        "memory": "Planner disposition was MONITOR.",
                        "user_id": "capacity-customer-42",
                        "created_at": "2026-08-30T10:00:00Z",
                        "updated_at": "2026-08-30T10:00:00Z",
                        "metadata": {
                            "event_type": "PLANNER_DECISION",
                            "company_id": 42,
                            "source": "capacity_planner_postgres_outbox",
                            "audit_reference": "case-1",
                            "secret": "must not leak",
                        },
                        "provider_internal": "must not leak",
                    },
                    {
                        "id": "other-app-memory",
                        "memory": "Not CapacityPilot data",
                        "metadata": {"source": "another_application"},
                    },
                ]
            }
        }
    )
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.setattr(memory, "get_settings", settings)

    result = memory.list_application_memories()

    assert result["status"] == "AVAILABLE"
    assert result["truncated"] is False
    assert result["items"] == [
        {
            "memory_id": "memory-1",
            "memory": "Planner disposition was MONITOR.",
            "user_id": "capacity-customer-42",
            "created_at": "2026-08-30T10:00:00Z",
            "updated_at": "2026-08-30T10:00:00Z",
            "metadata": {
                "event_type": "PLANNER_DECISION",
                "company_id": 42,
                "source": "capacity_planner_postgres_outbox",
                "audit_reference": "case-1",
            },
        }
    ]
    assert client.get_all_calls == [
        {
            "filters": {"user_id": "capacity-customer-42"},
            "page": 1,
            "page_size": 100,
        }
    ]


def test_list_application_memories_reports_provider_failure(monkeypatch):
    monkeypatch.setattr(memory, "_client", lambda: FakeClient(error=TimeoutError("slow")))
    monkeypatch.setattr(memory, "get_settings", settings)

    result = memory.list_application_memories()

    assert result["status"] == "DEGRADED"
    assert result["items"] == []
    assert result["errors"][0]["type"] == "TimeoutError"


def test_planner_memory_excludes_notes_and_raw_evidence(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.setattr(memory, "get_settings", settings)

    memory.add_outbox_memory(
        42,
        "PLANNER_DECISION",
        {
            "case_id": "case-1",
            "decision": "MONITOR",
            "likelihood_band": "HIGH",
            "confidence": "MEDIUM",
        },
    )

    call = client.add_call
    assert call["user_id"] == "capacity-customer-42"
    assert call["async_mode"] is False
    assert "MONITOR" in call["messages"][0]["content"]
    assert set(call["metadata"]) == {
        "event_type",
        "company_id",
        "source",
        "audit_reference",
    }
