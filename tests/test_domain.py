import pytest

from cpa_billing.domain import NANO_USD, largest_remainder, parse_tiers, tiered_weight


def test_tiered_weight_is_progressive() -> None:
    tiers = parse_tiers([{"left": 0, "right": 300, "multiplier": 1}, {"left": 300, "right": None, "multiplier": .5}])
    assert tiered_weight(500 * NANO_USD, tiers) == 400 * NANO_USD


def test_largest_remainder_preserves_total() -> None:
    allocated = largest_remainder(100, {1: 1, 2: 1, 3: 1})
    assert allocated == {1: 34, 2: 33, 3: 33}
    assert sum(allocated.values()) == 100


def test_negative_adjustment_allocation_preserves_total() -> None:
    assert sum(largest_remainder(-101, {1: 2, 2: 1}).values()) == -101


def test_tier_parser_rejects_rows_after_open_ended_tier() -> None:
    with pytest.raises(ValueError, match="last tier"):
        parse_tiers([
            {"left": 0, "right": None, "multiplier": 1},
            {"left": 10, "right": None, "multiplier": 1},
        ])
