from capacity_planner.models import Recommendation
from capacity_planner.nebius import normalize_recommendation


def test_recommendation_rejects_unknown_action():
    try:
        Recommendation(
            likelihood_pct=90,
            confidence="HIGH",
            action="PROVISION_CAPACITY",
            reasons=["test"],
        )
    except ValueError:
        return
    raise AssertionError("Unsafe action was accepted")


def test_model_output_is_normalized_to_safe_contract():
    result = normalize_recommendation(
        {
            "likelihood_pct": 90,
            "confidence": 0.8,
            "action": "PROVISION_CAPACITY",
            "reasons": ["growth"],
        }
    )
    assert result["confidence"] == "HIGH"
    assert result["action"] == "PLANNER_REVIEW"


def test_low_likelihood_unsafe_action_cannot_escape_allowlist():
    result = normalize_recommendation(
        {
            "likelihood_pct": 10,
            "confidence": "unexpected",
            "action": "DELETE_DATA",
            "reasons": ["bad model output"],
        }
    )
    assert result["confidence"] == "LOW"
    assert result["action"] == "MONITOR"


def test_missing_model_fields_are_rejected():
    try:
        normalize_recommendation({"likelihood_pct": 90})
    except ValueError as error:
        assert "missing fields" in str(error)
        return
    raise AssertionError("Malformed model output was accepted")
