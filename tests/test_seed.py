from capacity_planner.seed import (
    CUSTOMER_REGIONS,
    DEFAULT_CUSTOMER_COUNT,
    customer_region,
    synthetic_signal,
)


def test_default_customer_seed_count_is_100():
    assert DEFAULT_CUSTOMER_COUNT == 100


def test_synthetic_signals_are_deterministic_and_valid():
    first = synthetic_signal(320193)
    second = synthetic_signal(320193)
    assert first == second
    installed, consumed, *_ = first
    assert 0 <= consumed <= installed


def test_customer_region_is_stable_and_from_supported_demo_regions():
    assert customer_region(320193) == customer_region(320193)
    assert customer_region(320193) in CUSTOMER_REGIONS
