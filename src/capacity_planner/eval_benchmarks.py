"""Production benchmark definitions and scorecard calculations."""


def _percentage(numerator, denominator) -> float | None:
    if not denominator:
        return None
    return round(100 * float(numerator or 0) / float(denominator), 2)


def _row(
    name: str,
    category: str,
    measured: float | None,
    benchmark: float,
    samples: int,
    definition: str,
    *,
    status: str | None = None,
) -> dict:
    resolved_status = status
    if resolved_status is None:
        resolved_status = (
            "NOT EVALUATED"
            if measured is None
            else "PASS"
            if measured >= benchmark
            else "FAIL"
        )
    return {
        "eval": name,
        "category": category,
        "measured_pct": measured,
        "benchmark_pct": benchmark,
        "samples": int(samples or 0),
        "status": resolved_status,
        "definition": definition,
    }


def build_eval_scorecard(report: dict, *, api_connectivity_passed: bool) -> list[dict]:
    """Build comparable production eval rows from persisted measurements."""
    quality = report.get("data_quality", {})
    evaluation = report.get("evaluation", {})
    orchestration = report.get("orchestration", {})
    memory = report.get("memory", {})

    quality_runs = int(quality.get("total_runs") or 0)
    evaluation_runs = int(evaluation.get("evaluation_runs") or 0)
    predicted_positive = int(evaluation.get("predicted_positive") or 0)
    terminal_cases = int(orchestration.get("completed_cases") or 0) + int(
        orchestration.get("failed_cases") or 0
    )
    delivery_events = int(memory.get("delivery_events") or 0)

    chatbot = evaluation.get("chatbot_contract") or {}
    ui_action = evaluation.get("ui_action_contract") or {}
    ui_link = evaluation.get("ui_link_contract") or {}
    jira = evaluation.get("jira_handoff") or {}
    jira_completed = int(jira.get("completed_requests") or 0)
    jira_status = str(jira.get("mandatory_check_status") or "NOT_EVALUATED").replace(
        "_", " "
    )

    return [
        _row(
            "API and PostgreSQL connectivity",
            "Runtime",
            100.0 if api_connectivity_passed else 0.0,
            100.0,
            1,
            "The UI health probe must reach the API and its PostgreSQL dependency.",
        ),
        _row(
            "Technical data-quality pass rate",
            "Data quality",
            _percentage(quality.get("technical_passed_runs"), quality_runs),
            95.0,
            quality_runs,
            "At least 95% of agent runs must pass completeness, freshness, validity, and consistency checks.",
        ),
        _row(
            "Average specialist evidence coverage",
            "Evidence",
            (
                float(evaluation["average_evidence_coverage_pct"])
                if evaluation.get("average_evidence_coverage_pct") is not None
                else None
            ),
            95.0,
            evaluation_runs,
            "Specialist-agent evidence coverage must average at least 95% across evaluated runs.",
        ),
        _row(
            "Full specialist coverage rate",
            "Evidence",
            _percentage(evaluation.get("full_coverage_runs"), evaluation_runs),
            95.0,
            evaluation_runs,
            "At least 95% of evaluated runs must contain every required specialist result.",
        ),
        _row(
            "Expansion prediction precision",
            "Model quality",
            (
                float(evaluation["precision_pct"])
                if evaluation.get("precision_pct") is not None
                else None
            ),
            80.0,
            predicted_positive,
            "At least 80% of predicted-positive customers must expand within the forecast window.",
        ),
        _row(
            "Chatbot grounding contract",
            "Safety contract",
            _percentage(chatbot.get("passed_checks"), chatbot.get("total_checks")),
            100.0,
            chatbot.get("total_checks") or 0,
            "All transactional and follow-up intents must remain grounded in the correct audited records.",
            status=chatbot.get("status") if chatbot else "NOT EVALUATED",
        ),
        _row(
            "Navigation and action contract",
            "Safety contract",
            _percentage(ui_action.get("passed_checks"), ui_action.get("total_checks")),
            100.0,
            ui_action.get("total_checks") or 0,
            "All pages must be configured and reservation guardrails must select the correct action state.",
            status=ui_action.get("status") if ui_action else "NOT EVALUATED",
        ),
        _row(
            "Safe-link contract",
            "Safety contract",
            _percentage(ui_link.get("passed_checks"), ui_link.get("total_checks")),
            100.0,
            ui_link.get("total_checks") or 0,
            "All Jira and evidence-link construction checks must pass, including unsafe URL rejection.",
            status=ui_link.get("status") if ui_link else "NOT EVALUATED",
        ),
        _row(
            "Completed Jira ticket-link integrity",
            "Integration",
            _percentage(jira.get("completed_with_valid_link"), jira_completed),
            100.0,
            jira_completed,
            "Every completed Jira handoff must expose a valid HTTPS URL matching its project and issue key.",
            status=jira_status,
        ),
        _row(
            "Orchestration terminal success rate",
            "Reliability",
            (
                float(orchestration["terminal_success_rate_pct"])
                if orchestration.get("terminal_success_rate_pct") is not None
                else None
            ),
            99.0,
            terminal_cases,
            "At least 99% of terminal workflows must complete or route to planner review without failure.",
        ),
        _row(
            "Memory delivery success rate",
            "Reliability",
            (
                float(memory["delivery_success_rate_pct"])
                if memory.get("delivery_success_rate_pct") is not None
                else None
            ),
            95.0,
            delivery_events,
            "At least 95% of queued planner-memory events must be delivered successfully.",
        ),
    ]


def detailed_contract_checks(evaluation: dict) -> list[dict]:
    """Flatten every deterministic contract assertion for UI inspection."""
    suites = (
        ("Chatbot grounding", evaluation.get("chatbot_contract") or {}),
        ("Navigation and actions", evaluation.get("ui_action_contract") or {}),
        ("Safe links", evaluation.get("ui_link_contract") or {}),
    )
    return [
        {
            "suite": suite,
            "check": name.replace("_", " ").title(),
            "result": "PASS" if passed else "FAIL",
        }
        for suite, contract in suites
        for name, passed in (contract.get("checks") or {}).items()
    ]
