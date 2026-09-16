import pytest

from cpa_billing.domain import NANO_USD, format_yuan_per_usd, largest_remainder, parse_tiers, prorate_subscription_cost, tiered_weight


def test_tiered_weight_is_progressive() -> None:
    tiers = parse_tiers([{"left": 0, "right": 300, "multiplier": 1}, {"left": 300, "right": None, "multiplier": .5}])
    assert tiered_weight(500 * NANO_USD, tiers) == 400 * NANO_USD


def test_largest_remainder_preserves_total() -> None:
    allocated = largest_remainder(100, {1: 1, 2: 1, 3: 1})
    assert allocated == {1: 34, 2: 33, 3: 33}
    assert sum(allocated.values()) == 100


def test_negative_adjustment_allocation_preserves_total() -> None:
    assert sum(largest_remainder(-101, {1: 2, 2: 1}).values()) == -101


def test_format_yuan_per_usd_uses_integer_units_and_handles_zero_usage() -> None:
    assert format_yuan_per_usd(3, NANO_USD) == "0.030000"
    assert format_yuan_per_usd(3, 0) is None


def test_tier_parser_rejects_rows_after_open_ended_tier() -> None:
    with pytest.raises(ValueError, match="last tier"):
        parse_tiers([
            {"left": 0, "right": None, "multiplier": 1},
            {"left": 10, "right": None, "multiplier": 1},
        ])


def test_one_time_subscription_is_prorated_by_overlap() -> None:
    amount = prorate_subscription_cost(
        mode="one_time",
        period_cost_cents=10000,
        start_ms=0,
        end_ms=10_000,
        recurring_unit=None,
        recurring_interval=None,
        cycle_start_ms=0,
        cycle_end_ms=4_000,
        timezone="Asia/Shanghai",
    )
    assert amount == 4000


def test_recurring_monthly_subscription_uses_calendar_month_length() -> None:
    amount = prorate_subscription_cost(
        mode="recurring",
        period_cost_cents=3100,
        start_ms=0,
        end_ms=None,
        recurring_unit="month",
        recurring_interval=1,
        cycle_start_ms=0,
        cycle_end_ms=15 * 86_400_000,
        timezone="UTC",
    )
    assert amount == 1500


def test_subscription_outside_cycle_is_zero() -> None:
    amount = prorate_subscription_cost(
        mode="one_time",
        period_cost_cents=10000,
        start_ms=0,
        end_ms=1_000,
        recurring_unit=None,
        recurring_interval=None,
        cycle_start_ms=5_000,
        cycle_end_ms=8_000,
        timezone="UTC",
    )
    assert amount == 0
