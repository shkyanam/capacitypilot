import pytest

from capacity_planner import news_worker


def test_news_worker_finishes_successful_job(monkeypatch):
    job = {"company_id": 2, "attempt_count": 1}
    finished = []
    monkeypatch.setattr(news_worker, "claim_job", lambda _worker: job)
    monkeypatch.setattr(
        news_worker,
        "collect_news",
        lambda _company_id: {"status": "AVAILABLE", "items": [{"title": "evidence"}]},
    )
    monkeypatch.setattr(news_worker, "finish_job", lambda *args: finished.append(args))
    assert news_worker.run_once("worker") is True
    assert finished[0][0] == job
    assert finished[0][1]["status"] == "AVAILABLE"


def test_news_worker_retries_failed_job(monkeypatch):
    job = {"company_id": 2, "attempt_count": 1}
    failed = []
    monkeypatch.setattr(news_worker, "claim_job", lambda _worker: job)
    monkeypatch.setattr(
        news_worker,
        "collect_news",
        lambda _company_id: (_ for _ in ()).throw(TimeoutError("slow")),
    )
    monkeypatch.setattr(news_worker, "fail_job", lambda *args: failed.append(args))
    assert news_worker.run_once("worker") is True
    assert isinstance(failed[0][1], TimeoutError)


def test_news_worker_is_idle_when_queue_empty(monkeypatch):
    monkeypatch.setattr(news_worker, "claim_job", lambda _worker: None)
    assert news_worker.run_once("worker") is False


def test_bulk_worker_rejects_placeholder_sec_identity():
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        news_worker.validate_sec_user_agent("CapacityPlanner/1.0 admin@example.com")


def test_bulk_worker_accepts_declared_contact():
    assert news_worker.validate_sec_user_agent("ExampleCorp CapacityAgent ops@examplecorp.com") is None
