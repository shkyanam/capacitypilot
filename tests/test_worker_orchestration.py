from capacity_planner import worker


class SuccessfulGraph:
    def __init__(self):
        self.state = None

    def invoke(self, state):
        self.state = state
        return {**state, "recommendation": {"action": "PLANNER_REVIEW"}}


class FailingGraph:
    def invoke(self, _state):
        raise TimeoutError("provider timed out")


def test_idle_worker_does_nothing(monkeypatch):
    monkeypatch.setattr(worker, "claim_case", lambda _worker_id: None)
    assert worker.run_once("worker-1") is False


def test_worker_runs_graph_and_finishes_case(monkeypatch):
    graph = SuccessfulGraph()
    finished = []
    monkeypatch.setattr(
        worker,
        "claim_case",
        lambda _worker_id: {"case_id": "case-1", "company_id": 42, "attempt_count": 1},
    )
    monkeypatch.setattr(worker, "AGENT_GRAPH", graph)
    monkeypatch.setattr(worker, "finish_case", lambda *args: finished.append(args))

    assert worker.run_once("worker-1") is True
    assert graph.state["company_id"] == 42
    assert finished == [("case-1", {"action": "PLANNER_REVIEW"})]


def test_worker_records_error_and_delegates_retry_transition(monkeypatch):
    case = {"case_id": "case-2", "company_id": 7, "attempt_count": 1}
    events = []
    failures = []
    monkeypatch.setattr(worker, "claim_case", lambda _worker_id: case)
    monkeypatch.setattr(worker, "AGENT_GRAPH", FailingGraph())
    monkeypatch.setattr(worker, "event", lambda *args: events.append(args))
    monkeypatch.setattr(worker, "fail_case", lambda *args: failures.append(args))

    assert worker.run_once("worker-1") is True
    assert events[0][1] == "error"
    assert events[0][2]["type"] == "TimeoutError"
    assert failures[0][0] == case
    assert isinstance(failures[0][1], TimeoutError)
