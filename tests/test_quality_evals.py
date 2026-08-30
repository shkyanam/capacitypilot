from contextlib import contextmanager

from capacity_planner import api, repository


class Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row


class FakeConnection:
    def __init__(self, responses):
        self.responses = iter(responses)

    def execute(self, _sql, _params=()):
        return Cursor(next(self.responses))


def test_quality_eval_status_calculates_precision_and_orchestration_success(monkeypatch):
    responses = [
        {
            "total_runs": 10,
            "average_quality_pct": 95,
            "average_technical_quality_pct": 100,
            "technical_passed_runs": 10,
            "production_eligible_runs": 8,
            "last_run_at": None,
        },
        [{"check_name": "identity_complete", "run_count": 10, "passed_count": 10}],
        [],
        {
            "evaluation_runs": 10,
            "average_evidence_coverage_pct": 100,
            "full_coverage_runs": 10,
            "last_evaluation_at": None,
        },
        [],
        {"labeled": 10, "true_positive": 8, "predicted_positive": 10},
        {
            "total_cases": 12,
            "completed_cases": 9,
            "failed_cases": 1,
            "queued_cases": 1,
            "running_cases": 1,
            "retry_cases": 0,
            "average_attempts": 1.1,
            "average_duration_seconds": 12,
            "last_activity_at": None,
        },
        [],
        [],
        {
            "cases_started_24h": 12,
            "cases_completed_24h": 9,
            "cases_failed_24h": 1,
            "retried_cases": 2,
            "average_terminal_latency_seconds": 8,
            "p95_terminal_latency_seconds": 15,
            "oldest_queue_age_seconds": 5,
            "stale_running_cases": 0,
            "last_observed_at": None,
        },
        [],
        [],
        {
            "searches": 10,
            "searches_with_results": 4,
            "memories_returned": 7,
            "degraded_searches": 1,
            "last_search_at": None,
        },
        [{"status": "AVAILABLE", "search_count": 9, "last_search_at": None}],
        {
            "delivery_events": 5,
            "delivered": 4,
            "pending": 0,
            "failed": 1,
            "retried": 1,
            "delivery_attempts": 6,
            "last_delivered_at": None,
        },
        [{"status": "COMPLETE", "event_count": 4, "last_updated_at": None}],
        [],
        {
            "total_requests": 5,
            "completed_requests": 5,
            "pending_requests": 0,
            "failed_requests": 0,
            "completed_with_valid_link": 5,
            "last_checked_at": None,
        },
        [],
    ]

    @contextmanager
    def connection():
        yield FakeConnection(responses)

    monkeypatch.setattr(repository, "connection", connection)
    result = repository.quality_eval_status()

    assert result["evaluation"]["precision_pct"] == 80
    assert result["evaluation"]["precision_target_met"] is True
    assert result["orchestration"]["terminal_success_rate_pct"] == 90
    assert result["observability"]["p95_terminal_latency_seconds"] == 15
    assert result["memory"]["search_hit_rate_pct"] == 40
    assert result["memory"]["delivery_success_rate_pct"] == 80
    assert result["evaluation"]["jira_handoff"]["mandatory_check_status"] == "PASS"


def test_quality_evals_api_returns_repository_report(monkeypatch):
    expected = {
        "data_quality": {},
        "evaluation": {},
        "orchestration": {},
        "observability": {},
        "memory": {},
    }
    monkeypatch.setattr(api, "quality_eval_status", lambda: expected)

    assert api.quality_evals() == expected
