"""Deterministic UI action contracts shared by Streamlit and application evals."""

NAVIGATION_PAGES = (
    "Planner inbox",
    "Customer portfolio",
    "Ask CapacityPilot",
    "Capacity supply",
    "Reservations",
    "Jira handoffs",
    "Slack delivery",
    "Investigate customer",
    "Quality & evals",
    "System health",
)


def reservation_action_state(*, test_scenario: bool, capacity_sufficient: bool) -> dict:
    """Return whether reservation is allowed and the user-facing reason."""
    if test_scenario:
        return {
            "enabled": False,
            "reason_code": "PLANNING_SIMULATION",
            "message": (
                "Reservation is disabled because this recommendation is a planning "
                "simulation. Select a Source data recommendation to reserve capacity."
            ),
        }
    if not capacity_sufficient:
        return {
            "enabled": False,
            "reason_code": "INSUFFICIENT_CAPACITY",
            "message": (
                "Reservation is disabled because the selected regional pool does not "
                "have enough capacity. Create a HUB infrastructure order instead."
            ),
        }
    return {
        "enabled": True,
        "reason_code": None,
        "message": "Reservation is available after explicit planner confirmation.",
    }


def evaluate_ui_action_contract() -> dict:
    """Exercise navigation and high-risk action-state invariants."""
    simulation = reservation_action_state(
        test_scenario=True, capacity_sufficient=True
    )
    source_data = reservation_action_state(
        test_scenario=False, capacity_sufficient=True
    )
    shortfall = reservation_action_state(
        test_scenario=False, capacity_sufficient=False
    )
    checks = {
        "all_navigation_pages_configured": len(NAVIGATION_PAGES) == 10,
        "navigation_pages_are_unique": len(set(NAVIGATION_PAGES)) == len(NAVIGATION_PAGES),
        "simulation_reservation_is_disabled": simulation["enabled"] is False,
        "simulation_disable_reason_is_visible": bool(simulation["message"]),
        "source_data_reservation_is_enabled": source_data["enabled"] is True,
        "capacity_shortfall_reservation_is_disabled": shortfall["enabled"] is False,
        "capacity_shortfall_routes_to_hub": "HUB" in shortfall["message"],
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "passed_checks": len(checks) - len(failed),
        "total_checks": len(checks),
        "failed_checks": failed,
        "checks": checks,
    }
