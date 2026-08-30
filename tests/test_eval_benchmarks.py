from capacity_planner.eval_benchmarks import (
    build_eval_scorecard,
    detailed_contract_checks,
)


def sample_report():
    passing_contract = {
        "status": "PASS",
        "passed_checks": 2,
        "total_checks": 2,
        "checks": {"first_check": True, "second_check": True},
    }
    return {
        "data_quality": {"total_runs": 100, "technical_passed_runs": 96},
        "evaluation": {
            "evaluation_runs": 100,
            "average_evidence_coverage_pct": 98,
            "full_coverage_runs": 97,
            "precision_pct": 80,
            "predicted_positive": 20,
            "chatbot_contract": passing_contract,
            "ui_action_contract": passing_contract,
            "ui_link_contract": passing_contract,
            "jira_handoff": {
                "completed_requests": 6,
                "completed_with_valid_link": 6,
                "mandatory_check_status": "PASS",
            },
        },
        "orchestration": {
            "completed_cases": 99,
            "failed_cases": 1,
            "terminal_success_rate_pct": 99,
        },
        "memory": {"delivery_events": 20, "delivery_success_rate_pct": 95},
    }


def test_scorecard_lists_every_defined_eval_and_benchmark():
    rows = build_eval_scorecard(sample_report(), api_connectivity_passed=True)

    assert len(rows) == 11
    assert all(row["benchmark_pct"] > 0 for row in rows)
    assert all(row["definition"] for row in rows)
    assert all(row["status"] == "PASS" for row in rows)


def test_scorecard_marks_missing_prediction_labels_not_evaluated():
    report = sample_report()
    report["evaluation"]["precision_pct"] = None
    report["evaluation"]["predicted_positive"] = 0

    row = next(
        item
        for item in build_eval_scorecard(report, api_connectivity_passed=True)
        if item["eval"] == "Expansion prediction precision"
    )

    assert row["measured_pct"] is None
    assert row["samples"] == 0
    assert row["status"] == "NOT EVALUATED"


def test_contract_detail_lists_every_assertion():
    evaluation = sample_report()["evaluation"]

    rows = detailed_contract_checks(evaluation)

    assert len(rows) == 6
    assert {row["suite"] for row in rows} == {
        "Chatbot grounding",
        "Navigation and actions",
        "Safe links",
    }
    assert all(row["result"] == "PASS" for row in rows)
