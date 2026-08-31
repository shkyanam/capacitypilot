from capacity_planner import agents


def test_graph_executes_specialists_in_order_and_preserves_state(monkeypatch):
    order = []

    def specialist(name):
        def node(state):
            order.append(name)
            return {"evidence": {**state["evidence"], name: {"completed": True}}}

        return node

    def recommendation(state):
        order.append("recommend")
        assert set(state["evidence"]) == {
            "data_quality",
            "storage_history",
            "demand",
            "news",
            "evaluation",
            "memory",
        }
        return {"recommendation": {"action": "PLANNER_REVIEW"}}

    for name in ("data_quality", "storage_history", "demand", "news", "evaluation", "memory"):
        monkeypatch.setattr(agents, name, specialist(name))
    monkeypatch.setattr(agents, "recommend", recommendation)

    result = agents.build_graph().invoke(
        {
            "case_id": "case-1",
            "company_id": 1,
            "evidence": {},
            "recommendation": {},
            "errors": [],
        }
    )

    assert order == [
        "data_quality",
        "storage_history",
        "demand",
        "news",
        "evaluation",
        "memory",
        "recommend",
    ]
    assert result["recommendation"]["action"] == "PLANNER_REVIEW"


class FakeNebius:
    def recommendation(self, _evidence):
        return {
            "likelihood_pct": 95,
            "confidence": "HIGH",
            "action": "PLANNER_REVIEW",
            "reasons": ["test evidence"],
        }


class FakeMediumNebius:
    def recommendation(self, _evidence):
        return {
            "likelihood_pct": 80,
            "confidence": "MEDIUM",
            "action": "PLANNER_REVIEW",
            "reasons": ["moderate evidence"],
        }


class FakeLowNebius:
    def recommendation(self, _evidence):
        return {
            "likelihood_pct": 80,
            "confidence": "LOW",
            "action": "PLANNER_REVIEW",
            "reasons": ["cautious model response"],
        }


def recommendation_state(*, quality_passed=True, news_status="AVAILABLE"):
    return {
        "case_id": "case-1",
        "company_id": 1,
        "evidence": {
            "data_quality": {"passed": quality_passed},
            "news": {"status": news_status},
        },
        "recommendation": {},
        "errors": [],
    }


def test_quality_failure_forces_review_and_suppresses_alert(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(recommendation_state(quality_passed=False))["recommendation"]
    assert result["confidence"] == "LOW"
    assert result["action"] == "PLANNER_REVIEW"
    assert result["alert_allowed"] is False
    assert result["requires_human_approval"] is True


def test_degraded_news_forces_review_and_suppresses_alert(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(recommendation_state(news_status="DEGRADED"))["recommendation"]
    assert result["confidence"] == "LOW"
    assert result["action"] == "PLANNER_REVIEW"
    assert result["alert_allowed"] is False


def test_high_quality_high_confidence_case_can_enable_alert(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(recommendation_state())["recommendation"]
    assert result["alert_allowed"] is True
    assert result["requires_human_approval"] is True


def test_high_quality_medium_confidence_case_can_enable_alert(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeMediumNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(recommendation_state())["recommendation"]
    assert result["confidence"] == "MEDIUM"
    assert result["alert_allowed"] is True
    assert result["requires_human_approval"] is True


def test_healthy_demo_evidence_has_a_medium_confidence_floor(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeLowNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    current = recommendation_state()
    current["evidence"]["data_quality"] = {
        "passed": False,
        "technical_quality_passed": True,
        "production_eligible": False,
    }
    result = agents.recommend(current)["recommendation"]
    assert result["confidence"] == "MEDIUM"
    assert result["alert_allowed"] is True


def test_medium_confidence_still_cannot_bypass_failed_quality(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeMediumNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(
        recommendation_state(quality_passed=False)
    )["recommendation"]
    assert result["confidence"] == "LOW"
    assert result["alert_allowed"] is False


def test_medium_can_alert_when_technical_checks_pass(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeMediumNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    current = recommendation_state()
    current["evidence"]["data_quality"] = {
        "passed": False,
        "technical_quality_passed": True,
    }
    result = agents.recommend(current)["recommendation"]
    assert result["confidence"] == "MEDIUM"
    assert result["alert_allowed"] is True


def test_no_relevant_news_is_neutral_not_a_provider_failure(monkeypatch):
    monkeypatch.setattr(agents, "NebiusClient", FakeNebius)
    monkeypatch.setattr(agents, "event", lambda *_args: None)
    result = agents.recommend(
        recommendation_state(news_status="NO_RELEVANT_EVIDENCE")
    )["recommendation"]
    assert result["confidence"] == "HIGH"
    assert result["alert_allowed"] is True
