from contextlib import contextmanager
from types import SimpleNamespace

from capacity_planner import repository


class Cursor:
    def __init__(self, row=None, rowcount=0):
        self.row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row


class FakeConnection:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.responder(normalized, params)


def use_connection(monkeypatch, fake):
    @contextmanager
    def factory():
        yield fake

    monkeypatch.setattr(repository, "connection", factory)


def test_claim_case_uses_skip_locked_and_increments_attempt(monkeypatch):
    def responder(sql, _params):
        if sql.startswith("select * from capacity_planner.case_run"):
            return Cursor({"case_id": "case-1", "attempt_count": 0})
        if sql.startswith("update capacity_planner.case_run set status='RUNNING'"):
            return Cursor({"case_id": "case-1", "attempt_count": 1, "status": "RUNNING"})
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(max_agent_attempts=3))

    result = repository.claim_case("worker-1")
    assert result["attempt_count"] == 1
    assert "for update skip locked" in fake.calls[0][0].lower()
    assert "order by priority,created_at" in fake.calls[0][0].lower()
    assert fake.calls[1][1][0] == "worker-1"


def test_create_case_returns_existing_active_case_idempotently(monkeypatch):
    active = {"case_id": "existing", "company_id": 5, "status": "QUEUED"}

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select 1 from capacity_planner.company"):
            return Cursor({"exists": 1})
        if "status in ('QUEUED','RUNNING','RETRY')" in sql:
            return Cursor(active)
        raise AssertionError("A duplicate insert should not occur")

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    assert repository.create_case(5) == active
    assert "pg_advisory_xact_lock" in fake.calls[0][0]


def test_create_case_inserts_when_no_active_case_exists(monkeypatch):
    created = {"case_id": "new", "company_id": 5, "status": "QUEUED"}

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select 1 from capacity_planner.company"):
            return Cursor({"exists": 1})
        if "status in ('QUEUED','RUNNING','RETRY')" in sql:
            return Cursor(None)
        if sql.startswith("insert into capacity_planner.case_run"):
            return Cursor(created)
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    assert repository.create_case(5) == created
    assert "values (%s,%s,'QUEUED',10)" in fake.calls[-1][0]


def test_initial_portfolio_queues_only_companies_never_investigated(monkeypatch):
    companies = [{"company_id": 8}, {"company_id": 9}]

    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select c.company_id"):
            return Cursor(companies)
        if sql.startswith("insert into capacity_planner.case_run"):
            return Cursor()
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    assert repository.enqueue_initial_portfolio() == 2
    insert_calls = [call for call in fake.calls if call[0].startswith("insert into")]
    assert [call[1][1] for call in insert_calls] == [8, 9]
    assert all("values (%s,%s,'QUEUED',100)" in call[0] for call in insert_calls)
    assert "not exists" in fake.calls[1][0]
    assert "prior.company_id=c.company_id" in fake.calls[1][0]


def test_initial_portfolio_enqueue_is_idempotent_when_nothing_is_eligible(monkeypatch):
    def responder(sql, _params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select c.company_id"):
            return Cursor([])
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    assert repository.enqueue_initial_portfolio() == 0


def test_portfolio_refresh_queues_completed_customers_but_skips_active_ones(monkeypatch):
    companies = [{"company_id": 8}, {"company_id": 9}]

    def responder(sql, params):
        if "pg_advisory_xact_lock" in sql:
            return Cursor()
        if sql.startswith("select company_id from capacity_planner.company"):
            return Cursor(companies)
        if "status in ('QUEUED','RUNNING','RETRY')" in sql:
            return Cursor({"active": 1} if params[0] == 9 else None)
        if sql.startswith("insert into capacity_planner.case_run"):
            return Cursor()
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)

    assert repository.enqueue_portfolio_refresh(100) == 1
    insert_calls = [call for call in fake.calls if call[0].startswith("insert into")]
    assert len(insert_calls) == 1
    assert insert_calls[0][1][1] == 8


def test_portfolio_status_includes_last_refresh_and_progress(monkeypatch):
    refreshed = "2026-08-30T12:00:00+05:30"
    results = iter(
        [
            Cursor(
                {
                    "total_companies": 1000,
                    "scored_companies": 4,
                    "last_refresh_at": refreshed,
                }
            ),
            Cursor(
                [
                    {"status": "QUEUED", "case_count": 995},
                    {"status": "RUNNING", "case_count": 1},
                    {"status": "FAILED", "case_count": 2},
                ]
            ),
        ]
    )
    fake = FakeConnection(lambda _sql, _params: next(results))
    use_connection(monkeypatch, fake)

    result = repository.portfolio_status()

    assert result["scored_companies"] == 4
    assert result["remaining_companies"] == 996
    assert result["active_cases"] == 996
    assert result["failed_cases"] == 2
    assert result["last_refresh_at"] == refreshed
    assert result["baseline_complete"] is False


def test_forecast_overrides_are_append_only_and_audited(monkeypatch):
    def responder(sql, _params):
        if sql.startswith("select company_id from capacity_planner.case_run"):
            return Cursor({"company_id": 5})
        if sql.startswith("insert into capacity_planner.planner_forecast_override"):
            return Cursor()
        if sql.startswith("insert into capacity_planner.case_event"):
            return Cursor()
        raise AssertionError(sql)

    fake = FakeConnection(responder)
    use_connection(monkeypatch, fake)
    count = repository.save_forecast_overrides(
        [
            {
                "case_id": "case-1",
                "likelihood_pct": 72,
                "confidence": "MEDIUM",
                "timing_days": 120,
                "capacity_growth_tib": 300,
                "action": "PLANNER_REVIEW",
            }
        ],
        modified_by="planner@example.com",
        note="Adjusted using customer call",
    )

    assert count == 1
    assert len(fake.calls) == 3
    assert not any(call[0].startswith("update capacity_planner.case_run") for call in fake.calls)
    assert fake.calls[1][1][-2:] == ("Adjusted using customer call", "planner@example.com")
    assert "planner_forecast_override" in fake.calls[2][0]


def test_forecast_override_rejects_case_without_recommendation(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor(None))
    use_connection(monkeypatch, fake)
    try:
        repository.save_forecast_overrides(
            [
                {
                    "case_id": "missing",
                    "likelihood_pct": 70,
                    "confidence": "LOW",
                    "action": "MONITOR",
                }
            ],
            modified_by="planner",
            note="",
        )
    except LookupError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("Expected an invalid case to reject the transaction")


def test_claim_case_returns_none_for_empty_queue(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor(None))
    use_connection(monkeypatch, fake)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(max_agent_attempts=3))
    assert repository.claim_case("worker-1") is None
    assert len(fake.calls) == 1


def test_finish_case_persists_review_status(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor())
    use_connection(monkeypatch, fake)
    repository.finish_case("case-1", {"action": "PLANNER_REVIEW"})
    assert fake.calls[0][1][0] == "REVIEW_REQUIRED"
    assert '"action": "PLANNER_REVIEW"' in fake.calls[0][1][1]


def test_fail_case_retries_before_maximum_attempts(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor())
    use_connection(monkeypatch, fake)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(max_agent_attempts=3))
    repository.fail_case({"case_id": "case-1", "attempt_count": 2}, TimeoutError("slow"))
    assert fake.calls[0][1][0] == "RETRY"
    assert "TimeoutError: slow" in fake.calls[0][1][1]


def test_fail_case_becomes_terminal_at_maximum_attempts(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor())
    use_connection(monkeypatch, fake)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(max_agent_attempts=3))
    repository.fail_case({"case_id": "case-1", "attempt_count": 3}, RuntimeError("bad"))
    assert fake.calls[0][1][0] == "FAILED"


def test_stale_running_cases_are_returned_to_retry_queue(monkeypatch):
    fake = FakeConnection(lambda _sql, _params: Cursor(rowcount=2))
    use_connection(monkeypatch, fake)
    monkeypatch.setattr(repository, "get_settings", lambda: SimpleNamespace(stale_case_minutes=15))
    assert repository.recover_stale_cases() == 2
    sql, params = fake.calls[0]
    assert "set status='RETRY'" in sql
    assert "where status='RUNNING'" in sql
    assert params == (15,)
