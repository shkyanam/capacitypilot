from types import SimpleNamespace

import pytest

from capacity_planner import nebius


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    content = ""
    request = None

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        type(self).request = (url, kwargs)
        return FakeResponse(type(self).content)


def settings(api_key="secret"):
    return SimpleNamespace(
        nebius_api_key=api_key,
        nebius_base_url="https://nebius.example/v1",
        nebius_chat_model="test-model",
    )


def test_nebius_boundary_parses_fenced_json_and_sends_bearer_token(monkeypatch):
    FakeClient.content = """```json
{"likelihood_pct": 88, "confidence": "HIGH", "action": "PLANNER_REVIEW", "reasons": ["growth"]}
```"""
    monkeypatch.setattr(nebius.httpx, "Client", FakeClient)
    client = nebius.NebiusClient()
    client.settings = settings()
    result = client.recommendation({"storage": "evidence"})
    assert result["likelihood_pct"] == 88
    assert result["action"] == "PLANNER_REVIEW"
    url, request = FakeClient.request
    assert url == "https://nebius.example/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert request["json"]["temperature"] == 0


def test_nebius_boundary_requires_api_key():
    client = nebius.NebiusClient()
    client.settings = settings(api_key="")
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        client.recommendation({})
