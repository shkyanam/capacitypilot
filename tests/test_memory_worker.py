from capacity_planner import memory_worker


def test_idle_worker_returns_false(monkeypatch):
    monkeypatch.setattr(memory_worker, "claim_memory", lambda _worker: None)
    assert memory_worker.run_once("worker-1") is False


def test_worker_syncs_and_completes(monkeypatch):
    item = {
        "outbox_id": "outbox-1",
        "company_id": 42,
        "event_type": "PLANNER_DECISION",
        "payload": {"case_id": "case-1"},
    }
    completed = []
    monkeypatch.setattr(memory_worker, "claim_memory", lambda _worker: item)
    monkeypatch.setattr(memory_worker, "add_outbox_memory", lambda *_args: None)
    monkeypatch.setattr(memory_worker, "complete_memory", completed.append)

    assert memory_worker.run_once("worker-1") is True
    assert completed == ["outbox-1"]


def test_worker_records_provider_failure(monkeypatch):
    item = {
        "outbox_id": "outbox-1",
        "company_id": 42,
        "event_type": "PLANNER_DECISION",
        "payload": {"case_id": "case-1"},
    }
    failures = []
    monkeypatch.setattr(memory_worker, "claim_memory", lambda _worker: item)

    def fail_provider(*_args):
        raise TimeoutError("slow")

    monkeypatch.setattr(memory_worker, "add_outbox_memory", fail_provider)
    monkeypatch.setattr(memory_worker, "fail_memory", lambda value, error: failures.append((value, error)))

    assert memory_worker.run_once("worker-1") is True
    assert failures[0][0] == item
    assert isinstance(failures[0][1], TimeoutError)
