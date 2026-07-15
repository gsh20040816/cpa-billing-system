from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


NANO_USD = 1_000_000_000


@dataclass(frozen=True)
class Tier:
    left_nano: int
    right_nano: int | None
    multiplier_ppm: int


def tiered_weight(actual_nano: int, tiers: Iterable[Tier]) -> int:
    if actual_nano <= 0:
        return 0
    result = 0
    for tier in tiers:
        upper = tier.right_nano if tier.right_nano is not None else actual_nano
        segment = max(0, min(actual_nano, upper) - tier.left_nano)
        result += (segment * tier.multiplier_ppm + 500_000) // 1_000_000
        if tier.right_nano is None or actual_nano <= upper:
            break
    return result


def parse_tiers(items: list[dict[str, object]]) -> list[Tier]:
    tiers: list[Tier] = []
    expected = Decimal(0)
    for index, item in enumerate(items):
        left = Decimal(str(item["left"]))
        right_raw = item.get("right")
        multiplier = Decimal(str(item["multiplier"]))
        if left != expected or multiplier < 0:
            raise ValueError("tiers must be continuous and non-negative")
        right = None if right_raw is None else Decimal(str(right_raw))
        if right is not None and right <= left:
            raise ValueError("tier right boundary must exceed left boundary")
        tiers.append(
            Tier(
                int((left * NANO_USD).to_integral_value(rounding=ROUND_HALF_UP)),
                None if right is None else int((right * NANO_USD).to_integral_value(rounding=ROUND_HALF_UP)),
                int((multiplier * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP)),
            )
        )
        if right is None:
            if index != len(items) - 1:
                raise ValueError("only the last tier may be open ended")
            break
        expected = right
    if not tiers or tiers[-1].right_nano is not None:
        raise ValueError("last tier must be open ended")
    return tiers


def largest_remainder(total_cents: int, weights: dict[int, int]) -> dict[int, int]:
    positive = {key: value for key, value in weights.items() if value > 0}
    if total_cents == 0 or not positive:
        return {key: 0 for key in weights}
    total_weight = sum(positive.values())
    sign = 1 if total_cents >= 0 else -1
    cents = abs(total_cents)
    base: dict[int, int] = {}
    remainders: list[tuple[int, int]] = []
    for key, weight in positive.items():
        numerator = cents * weight
        base[key] = numerator // total_weight
        remainders.append((numerator % total_weight, key))
    missing = cents - sum(base.values())
    for _, key in sorted(remainders, key=lambda pair: (-pair[0], pair[1]))[:missing]:
        base[key] += 1
    return {key: sign * base.get(key, 0) for key in weights}


def format_usd_nano(value: int) -> str:
    return f"{Decimal(value) / Decimal(NANO_USD):,.4f}"


def format_cents(value: int) -> str:
    return f"{Decimal(value) / Decimal(100):,.2f}"


def format_yuan_per_usd(amount_cents: int, usage_nano_usd: int) -> str | None:
    if usage_nano_usd == 0:
        return None
    rate = Decimal(amount_cents) * Decimal(NANO_USD) / (Decimal(usage_nano_usd) * Decimal(100))
    return format(rate, ".6f")
