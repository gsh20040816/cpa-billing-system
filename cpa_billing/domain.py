from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo


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


def add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def overlap_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def iter_subscription_periods(
    start_ms: int,
    end_ms: int | None,
    unit: str,
    interval: int,
    until_ms: int,
    timezone: str,
) -> Iterator[tuple[int, int]]:
    zone = ZoneInfo(timezone)
    start = datetime.fromtimestamp(start_ms / 1000, zone)
    hard_end = None if end_ms is None else datetime.fromtimestamp(end_ms / 1000, zone)
    limit = datetime.fromtimestamp(until_ms / 1000, zone)
    index = 0
    while index <= 2400:
        if unit == "month":
            period_start = add_calendar_months(start, index * interval)
            period_end = add_calendar_months(start, (index + 1) * interval)
        else:
            period_start = start + timedelta(days=interval * index)
            period_end = start + timedelta(days=interval * (index + 1))
        if hard_end is not None:
            if period_start >= hard_end:
                return
            if period_end > hard_end:
                period_end = hard_end
        if period_start >= limit:
            return
        yield int(period_start.timestamp() * 1000), int(period_end.timestamp() * 1000)
        index += 1
    raise ValueError("subscription period iteration exceeded limit")


def prorate_subscription_cost(
    *,
    mode: str,
    period_cost_cents: int,
    start_ms: int,
    end_ms: int | None,
    recurring_unit: str | None,
    recurring_interval: int | None,
    cycle_start_ms: int,
    cycle_end_ms: int,
    timezone: str,
) -> int:
    if period_cost_cents < 0:
        raise ValueError("subscription cost cannot be negative")
    if cycle_end_ms <= cycle_start_ms or period_cost_cents == 0:
        return 0
    if mode == "one_time":
        if end_ms is None or end_ms <= start_ms:
            raise ValueError("one-time subscription requires a valid end time")
        periods = [(start_ms, end_ms)]
    elif mode == "recurring":
        unit = recurring_unit or "month"
        interval = int(recurring_interval or 1)
        if unit not in {"month", "day"} or interval < 1:
            raise ValueError("recurring subscription period is invalid")
        periods = [
            period
            for period in iter_subscription_periods(
                start_ms, end_ms, unit, interval, cycle_end_ms, timezone,
            )
            if period[1] > cycle_start_ms
        ]
    else:
        raise ValueError(f"unsupported subscription mode: {mode}")

    total = Decimal(0)
    for period_start, period_end in periods:
        duration = period_end - period_start
        overlap = overlap_ms(period_start, period_end, cycle_start_ms, cycle_end_ms)
        if duration <= 0 or overlap <= 0:
            continue
        total += Decimal(period_cost_cents) * Decimal(overlap) / Decimal(duration)
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))
