import httpx

from capacity_planner.runtime_health import evaluate_api_connectivity


class Response:
    def __init__(self, *, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error
        self.request = httpx.Request("GET", "http://localhost:8000/health")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


def test_api_connectivity_eval_passes_healthy_response():
    result = evaluate_api_connectivity(
        "http://localhost:8000",
        requester=lambda *_args, **_kwargs: Response(payload={"status": "ok"}),
    )

    assert result["status"] == "PASS"
    assert result["failure_type"] is None
    assert result["recovery_command"] is None


def test_api_connectivity_eval_identifies_connection_refused():
    def refused(url, **_kwargs):
        raise httpx.ConnectError("[Errno 61] Connection refused", request=httpx.Request("GET", url))

    result = evaluate_api_connectivity("http://localhost:8000", requester=refused)

    assert result["status"] == "FAIL"
    assert result["failure_type"] == "CONNECTION_REFUSED"
    assert result["recovery_command"] == "uv run capacity-api"


def test_api_connectivity_eval_identifies_http_failure():
    result = evaluate_api_connectivity(
        "http://localhost:8000",
        requester=lambda *_args, **_kwargs: Response(status_code=503),
    )

    assert result["status"] == "FAIL"
    assert result["failure_type"] == "HTTP_503"


def test_api_connectivity_eval_rejects_malformed_health_payload():
    result = evaluate_api_connectivity(
        "http://localhost:8000",
        requester=lambda *_args, **_kwargs: Response(payload={"status": "starting"}),
    )

    assert result["status"] == "FAIL"
    assert result["failure_type"] == "UNHEALTHY_RESPONSE"
