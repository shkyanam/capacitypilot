from contextlib import contextmanager
from types import SimpleNamespace

from capacity_planner import news_jobs


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=0):
        self.row = row
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return next(self.responses)


def patch_connection(monkeypatch, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(news_jobs, "connection", factory)


def settings():
    return SimpleNamespace(
        news_bulk_company_limit=100,
        news_bulk_refresh_hours=24,
        news_bulk_max_attempts=3,
        stale_case_minutes=15,
    )


def test_enqueue_all_is_idempotent_and_refresh_aware(monkeypatch):
    fake = FakeConnection([Cursor(rowcount=1000)])
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(news_jobs, "get_settings", settings)
    assert news_jobs.enqueue_all() == 1000
    assert "on conflict(company_id) do update" in fake.calls[0][0].lower()
    assert fake.calls[0][1] == (24,)


def test_enqueue_limited_force_targets_only_requested_customers(monkeypatch):
    fake = FakeConnection([Cursor(), Cursor(), Cursor(rowcount=100)])
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(news_jobs, "get_settings", settings)
    queued, run_id = news_jobs.enqueue_limited(100, force=True)
    assert queued == 100
    assert run_id
    sql, params = fake.calls[2]
    assert "selected_companies" in sql
    assert "status <> 'RUNNING'" in sql
    assert params == (100, run_id, True, 24)


def test_claim_job_uses_skip_locked(monkeypatch):
    fake = FakeConnection(
        [
            Cursor(row={"company_id": 2, "attempt_count": 0}),
            Cursor(row={"company_id": 2, "attempt_count": 1, "status": "RUNNING"}),
        ]
    )
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(news_jobs, "get_settings", settings)
    result = news_jobs.claim_job("news-worker")
    assert result["status"] == "RUNNING"
    assert "for update skip locked" in fake.calls[0][0].lower()


def test_finish_job_records_no_evidence(monkeypatch):
    fake = FakeConnection([Cursor()])
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(news_jobs, "get_settings", settings)
    news_jobs.finish_job(
        {"company_id": 2, "attempt_count": 1},
        {"status": "NO_RELEVANT_EVIDENCE", "items": [], "errors": []},
    )
    assert fake.calls[0][1][0] == "NO_EVIDENCE"
    assert fake.calls[0][1][1] == 0


def test_unavailable_provider_is_retried(monkeypatch):
    fake = FakeConnection([Cursor()])
    patch_connection(monkeypatch, fake)
    monkeypatch.setattr(news_jobs, "get_settings", settings)
    news_jobs.finish_job(
        {"company_id": 2, "attempt_count": 1},
        {"status": "UNAVAILABLE", "items": [], "errors": [{"provider": "SEC"}]},
    )
    assert fake.calls[0][1][0] == "RETRY"


def test_status_counts_reports_progress(monkeypatch):
    fake = FakeConnection(
        [
            Cursor(rows=[{"status": "COMPLETE", "count": 10}, {"status": "QUEUED", "count": 90}]),
            Cursor(row={"count": 25}),
        ]
    )
    patch_connection(monkeypatch, fake)
    result = news_jobs.status_counts()
    assert result == {
        "jobs": {"COMPLETE": 10, "QUEUED": 90},
        "total_jobs": 100,
        "processed_jobs": 10,
        "evidence_records": 25,
    }
