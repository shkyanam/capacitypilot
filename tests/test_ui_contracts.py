from capacity_planner.ui_contracts import (
    NAVIGATION_PAGES,
    evaluate_ui_action_contract,
    reservation_action_state,
)


def test_navigation_contract_has_all_unique_pages():
    assert len(NAVIGATION_PAGES) == 10
    assert len(set(NAVIGATION_PAGES)) == len(NAVIGATION_PAGES)


def test_simulation_reservation_is_disabled_with_reason():
    state = reservation_action_state(test_scenario=True, capacity_sufficient=True)

    assert state["enabled"] is False
    assert state["reason_code"] == "PLANNING_SIMULATION"
    assert "planning simulation" in state["message"]


def test_source_data_reservation_with_capacity_is_enabled():
    state = reservation_action_state(test_scenario=False, capacity_sufficient=True)

    assert state["enabled"] is True
    assert state["reason_code"] is None


def test_capacity_shortfall_routes_to_hub_instead_of_reservation():
    state = reservation_action_state(test_scenario=False, capacity_sufficient=False)

    assert state["enabled"] is False
    assert state["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert "HUB" in state["message"]


def test_ui_action_contract_passes():
    result = evaluate_ui_action_contract()

    assert result["status"] == "PASS"
    assert result["passed_checks"] == result["total_checks"] == 7
    assert result["failed_checks"] == []
