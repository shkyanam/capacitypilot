from datetime import UTC, datetime, timedelta
from decimal import Decimal

from capacity_planner.agents import evaluate_quality


def valid_signal():
    return {
        "company_name": "Example Corp",
        "sec_cik": 123,
        "ticker": "EXM",
        "installed_tib": Decimal(100),
        "consumed_tib": Decimal(80),
        "trailing_12m_growth_tib": Decimal(20),
        "prior_expansion_count": 2,
        "avg_prior_expansion_tib": Decimal(25),
        "open_demand_tib": Decimal(10),
        "demand_stage": "QUALIFIED",
        "source_freshness": "FRESH",
        "data_classification": "PRODUCTION",
        "generated_at": datetime.now(UTC),
        "duplicate_cik_count": 1,
        "duplicate_ticker_exchange_count": 1,
    }


def test_quality_runs_all_checks_and_passes_valid_production_data():
    result = evaluate_quality(valid_signal())
    assert result["check_count"] == 16
    assert result["quality_score_pct"] == 100.0
    assert result["technical_quality_score_pct"] == 100.0
    assert result["passed"] is True
    assert result["failed_checks"] == []


def test_quality_rejects_stale_inconsistent_and_synthetic_data():
    signal = valid_signal()
    signal.update(
        consumed_tib=Decimal(110),
        source_freshness="STALE",
        data_classification="SYNTHETIC_DEMO",
        generated_at=datetime.now(UTC) - timedelta(days=7),
    )
    result = evaluate_quality(signal)
    assert result["passed"] is False
    assert result["production_eligible"] is False
    assert result["quality_score_pct"] < 100
    assert "consumption_not_above_installed" in result["failed_checks"]
    assert "snapshot_within_age_limit" in result["failed_checks"]
    assert "source_marked_fresh" in result["failed_checks"]
    assert "production_data_only" in result["failed_checks"]


def test_quality_keeps_test_scenarios_out_of_production_eligibility():
    signal = valid_signal()
    signal["data_classification"] = "TEST_SCENARIO"

    result = evaluate_quality(signal)

    assert result["technical_quality_passed"] is True
    assert result["production_eligible"] is False
    assert result["failed_checks"] == ["production_data_only"]


def test_fresh_synthetic_demo_baseline_does_not_expire_by_wall_clock_age():
    signal = valid_signal()
    signal.update(
        data_classification="SYNTHETIC_DEMO",
        source_freshness="FRESH",
        generated_at=datetime.now(UTC) - timedelta(days=7),
    )

    result = evaluate_quality(signal)

    assert result["technical_quality_passed"] is True
    assert result["failed_checks"] == ["production_data_only"]


def test_quality_rejects_null_invalid_name_and_duplicates():
    signal = valid_signal()
    signal.update(
        company_name="<script>Bad Corp</script>",
        open_demand_tib=None,
        duplicate_cik_count=2,
        duplicate_ticker_exchange_count=2,
    )
    result = evaluate_quality(signal)
    assert "required_fields_have_no_nulls" in result["failed_checks"]
    assert "text_has_no_invalid_characters" in result["failed_checks"]
    assert "no_duplicate_sec_cik" in result["failed_checks"]
    assert "no_duplicate_ticker_on_exchange" in result["failed_checks"]
