from contextlib import contextmanager

from capacity_planner import api, portfolio_chat
from capacity_planner.models import PortfolioChatRequest, PortfolioQueryPlan


def test_portfolio_filter_values_are_parameterized():
    plan = PortfolioQueryPlan(
        operation="LIST",
        customer_search="Acme%'",
        regions=["APAC"],
        min_utilization_pct=85,
        sort_by="utilization_pct",
    )

    where, params = portfolio_chat._portfolio_sql(plan)

    assert "Acme" not in where
    assert "company_name ilike %s" in where
    assert "region = any(%s)" in where
    assert "utilization_pct >= %s" in where
    assert params == ["%Acme\\%'%", "%Acme\\%'%", ["APAC"], 85]


def test_fallback_understands_planner_review_count():
    plan = portfolio_chat.fallback_query_plan("How many customers need planner review?")

    assert plan.operation == "COUNT"
    assert plan.planner_states == ["NEEDS_REVIEW"]


def test_planner_review_count_bypasses_nebius(monkeypatch):
    class ForbiddenNebius:
        def __init__(self):
            raise AssertionError("Planner-review counts must not call Nebius")

    monkeypatch.setattr(portfolio_chat, "NebiusClient", ForbiddenNebius)
    monkeypatch.setattr(
        portfolio_chat,
        "query_portfolio",
        lambda plan: {
            "summary": {"matching_customers": 149},
            "rows": [],
        },
    )

    result = portfolio_chat.answer_portfolio_question(
        "How many customers need planner review?"
    )

    assert result["answer"] == "149 customers currently need planner review."
    assert result["interpretation_source"] == "DETERMINISTIC_PORTFOLIO"
    assert result["interpreted_as"]["planner_states"] == ["NEEDS_REVIEW"]


def test_reservation_audit_plan_parses_24_hour_window():
    plan = portfolio_chat.reservation_audit_plan(
        "How many reservations are approved in the last 24hrs?"
    )

    assert plan == {
        "intent": "RESERVATION_AUDIT_COUNT",
        "status": "LOCAL_RESERVED",
        "time_window_hours": 24,
        "scope": "ALL_PLANNERS",
    }


def test_chatbot_grounding_eval_prevents_reservation_portfolio_fallback():
    result = portfolio_chat.evaluate_portfolio_chat_contract()

    assert result["status"] == "PASS"
    assert result["passed_checks"] == result["total_checks"] == 8
    assert result["failed_checks"] == []


def test_reservation_audit_queries_authoritative_table_with_time_window(monkeypatch):
    executed = []

    class Cursor:
        def fetchone(self):
            return {
                "approved_reservations": 2,
                "customers": 2,
                "planner_identities": 1,
                "reserved_tib": 150,
                "latest_reservation_at": None,
            }

    class Connection:
        def execute(self, sql, params):
            executed.append((sql, params))
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(portfolio_chat, "connection", connection)

    result = portfolio_chat.query_reservation_audit(24)

    assert result["approved_reservations"] == 2
    assert "capacity_planner.local_capacity_reservation" in executed[0][0]
    assert "status='LOCAL_RESERVED'" in executed[0][0]
    assert "created_at >= now()-(%s * interval '1 hour')" in executed[0][0]
    assert executed[0][1] == [24]


def test_reservation_question_bypasses_nebius_and_generic_portfolio(monkeypatch):
    class ForbiddenNebius:
        def __init__(self):
            raise AssertionError("Reservation audit questions must not call Nebius")

    monkeypatch.setattr(portfolio_chat, "NebiusClient", ForbiddenNebius)
    monkeypatch.setattr(
        portfolio_chat,
        "query_portfolio",
        lambda _plan: (_ for _ in ()).throw(
            AssertionError("Reservation audit questions must not query the portfolio")
        ),
    )
    monkeypatch.setattr(
        portfolio_chat,
        "query_reservation_audit",
        lambda hours: {
            "approved_reservations": 2,
            "customers": 2,
            "planner_identities": 1,
            "reserved_tib": 150,
            "latest_reservation_at": None,
        },
    )

    result = portfolio_chat.answer_portfolio_question(
        "How many reservations are approved in the last 24hrs?"
    )

    assert result["interpretation_source"] == "DETERMINISTIC_AUDIT"
    assert result["interpreted_as"]["time_window_hours"] == 24
    assert result["answer"].startswith(
        "CapacityPilot records show 2 approved local capacity reservations"
    )


def test_display_all_followup_inherits_reservation_context(monkeypatch):
    rows = [
        {
            "reservation_id": "reservation-1",
            "company_name": "Example Co",
            "ticker": "EX",
            "region": "us-ashburn-1",
            "qfab": "QFAB-A",
            "requested_tib": 100,
            "target_date": "2026-09-30",
            "status": "LOCAL_RESERVED",
            "planner_identity": "planner@example.com",
            "created_at": "2026-08-30T10:00:00Z",
        }
    ]
    monkeypatch.setattr(portfolio_chat, "query_reservation_details", lambda hours: rows)
    monkeypatch.setattr(
        portfolio_chat,
        "query_portfolio",
        lambda _plan: (_ for _ in ()).throw(
            AssertionError("Contextual reservation follow-up must not query portfolio")
        ),
    )

    result = portfolio_chat.answer_portfolio_question(
        "Can you display all of them?",
        context={
            "previous_intent": "RESERVATION_AUDIT_COUNT",
            "previous_time_window_hours": 24,
        },
    )

    assert result["interpretation_source"] == "DETERMINISTIC_AUDIT"
    assert result["interpreted_as"]["intent"] == "RESERVATION_AUDIT_LIST"
    assert result["interpreted_as"]["context_inherited"] is True
    assert result["rows"] == rows
    assert result["answer"] == (
        "Here are all 1 approved local capacity reservations from the last 24 hours."
    )


def test_query_portfolio_uses_allowlisted_sort_and_returns_rows(monkeypatch):
    executed = []

    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Connection:
        def execute(self, sql, params):
            executed.append((sql, params))
            if "matching_customers" in sql:
                return Cursor(
                    {
                        "matching_customers": 1,
                        "average_utilization_pct": 90,
                        "total_open_demand_tib": 25,
                        "average_likelihood_pct": 80,
                        "total_suggested_growth_tib": 30,
                    }
                )
            return Cursor([{"company_name": "Example Co", "utilization_pct": 90}])

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(portfolio_chat, "connection", connection)
    result = portfolio_chat.query_portfolio(
        PortfolioQueryPlan(
            operation="LIST",
            sort_by="utilization_pct",
            sort_direction="DESC",
            limit=5,
        )
    )

    assert result["summary"]["matching_customers"] == 1
    assert result["rows"][0]["company_name"] == "Example Co"
    assert "order by utilization_pct DESC nulls last limit %s" in executed[1][0]
    assert executed[1][1] == (5,)


def test_answer_portfolio_question_uses_validated_plan(monkeypatch):
    plan = PortfolioQueryPlan(operation="COUNT", min_utilization_pct=85)

    class FakeNebius:
        def portfolio_query_plan(self, _question):
            return plan

    monkeypatch.setattr(portfolio_chat, "NebiusClient", FakeNebius)
    monkeypatch.setattr(
        portfolio_chat,
        "query_portfolio",
        lambda _plan: {
            "summary": {
                "matching_customers": 12,
                "average_utilization_pct": 90,
                "total_open_demand_tib": 100,
                "average_likelihood_pct": 80,
                "total_suggested_growth_tib": 120,
            },
            "rows": [],
        },
    )

    result = portfolio_chat.answer_portfolio_question(
        "How many customers exceed 85% utilization?"
    )

    assert result["answer"] == "I found 12 customers matching those criteria."
    assert result["interpreted_as"]["min_utilization_pct"] == 85


def test_answer_falls_back_when_nebius_is_unavailable(monkeypatch):
    class UnavailableNebius:
        def portfolio_query_plan(self, _question):
            raise TimeoutError("provider slow")

    monkeypatch.setattr(portfolio_chat, "NebiusClient", UnavailableNebius)
    monkeypatch.setattr(
        portfolio_chat,
        "query_portfolio",
        lambda plan: {
            "summary": {
                "matching_customers": 3,
                "average_utilization_pct": 91,
                "total_open_demand_tib": 10,
                "average_likelihood_pct": 80,
                "total_suggested_growth_tib": 20,
            },
            "rows": [{"company_name": "Example"}] * plan.limit,
        },
    )

    result = portfolio_chat.answer_portfolio_question(
        "Which 5 customers have the highest utilization?"
    )

    assert result["interpretation_source"] == "SAFE_FALLBACK"
    assert result["interpreted_as"]["sort_by"] == "utilization_pct"
    assert result["interpreted_as"]["limit"] == 5
    assert result["interpreted_as"]["confidence"] == []


def test_portfolio_chat_api_delegates_to_grounded_service(monkeypatch):
    monkeypatch.setattr(
        api,
        "answer_portfolio_question",
        lambda question, context=None: {
            "answer": question,
            "rows": [],
            "context": context,
        },
    )

    result = api.portfolio_chat(PortfolioChatRequest(question="Show top customers"))

    assert result == {"answer": "Show top customers", "rows": [], "context": None}


def test_portfolio_chat_api_passes_validated_conversation_context(monkeypatch):
    captured = []
    monkeypatch.setattr(
        api,
        "answer_portfolio_question",
        lambda question, context=None: captured.append((question, context)) or {"rows": []},
    )

    api.portfolio_chat(
        PortfolioChatRequest(
            question="Show them",
            context={
                "previous_question": "How many reservations in the last 24 hours?",
                "previous_intent": "RESERVATION_AUDIT_COUNT",
                "previous_time_window_hours": 24,
            },
        )
    )

    assert captured == [
        (
            "Show them",
            {
                "previous_question": "How many reservations in the last 24 hours?",
                "previous_intent": "RESERVATION_AUDIT_COUNT",
                "previous_time_window_hours": 24,
            },
        )
    ]
