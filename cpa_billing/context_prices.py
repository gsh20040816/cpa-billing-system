"""Explicit, whole-request context prices; no locally derived multipliers."""
from __future__ import annotations

import json
from typing import Any

from .domain import decimal_units

FIELDS = ("input", "output", "cache_read", "cache_creation")
UPSTREAM_FIELDS = {"input": "input", "output": "output", "cache_read": "cache_read", "cache_creation": "cache_write"}
ROW_FIELDS = {"input": "prompt_per_1m", "output": "completion_per_1m", "cache_read": "cache_read_per_1m", "cache_creation": "cache_creation_per_1m"}


def nano_rate(value: Any) -> int:
    """USD per million tokens to nano-USD per token."""
    return decimal_units(str(value), 3)


def normalize_context_prices(prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(prices, list):
        raise ValueError("context prices must be an array")
    result, seen = [], set()
    for price in prices:
        threshold = price["threshold_tokens"]
        tier = price.get("service_tier", "default")
        if type(threshold) is not int or threshold <= 0:
            raise ValueError("context threshold must be a positive integer")
        if tier not in {"default", "priority", "flex"}:
            raise ValueError("unsupported context service tier")
        identity = (tier, threshold)
        if identity in seen:
            raise ValueError("duplicate context threshold for service tier")
        seen.add(identity)
        rates = {field: price[field] for field in FIELDS}
        if any(type(rate) is not int or rate < 0 for rate in rates.values()):
            raise ValueError("context prices must be non-negative integers")
        result.append({"service_tier": tier, "threshold_tokens": threshold, **rates})
    return sorted(result, key=lambda item: (item["service_tier"], item["threshold_tokens"]))


def cpamp_rules(row: Any) -> dict[str, Any]:
    rules: dict[str, Any] = {
        "long_threshold_tokens": None,
        "long_input_multiplier_ppm": 1_000_000,
        "long_output_multiplier_ppm": 1_000_000,
        "context_tiers_json": "[]",
    }
    if row["source"] != "models.dev":
        return rules
    raw = json.loads(row["raw_json"])
    base = {field: nano_rate(row[column]) for field, column in ROW_FIELDS.items()}
    contexts: list[dict[str, Any]] = []

    def context_prices(cost: dict[str, Any], service_tier: str, baseline: dict[str, int]) -> None:
        tiers = cost.get("tiers", [])
        if "context_over_200k" in cost and not tiers:
            raise ValueError("context prices have no explicit context threshold")
        for item in tiers:
            descriptor = item["tier"]
            if descriptor["type"] != "context":
                raise ValueError("unsupported pricing tier type")
            rates = dict(baseline)
            for field, upstream in UPSTREAM_FIELDS.items():
                if upstream in item:
                    rates[field] = nano_rate(item[upstream])
            contexts.append({"service_tier": service_tier, "threshold_tokens": descriptor["size"], **rates})

    context_prices(raw.get("cost", {}), "default", base)
    seen = set()
    for mode_name, mode in raw.get("experimental", {}).get("modes", {}).items():
        tier = mode.get("provider", {}).get("body", {}).get("service_tier")
        if tier is None and mode_name == "fast":
            tier = "priority"
        if tier not in {"priority", "flex"} or "cost" not in mode:
            continue
        if tier in seen:
            raise ValueError(f"multiple prices for service tier {tier}")
        seen.add(tier)
        rates = dict(base)
        for field, upstream in UPSTREAM_FIELDS.items():
            if upstream in mode["cost"]:
                rates[field] = nano_rate(mode["cost"][upstream])
                rules[f"{tier}_{field}_nano_per_token"] = rates[field]
        context_prices(mode["cost"], tier, rates)
    rules["context_tiers_json"] = json.dumps(normalize_context_prices(contexts), separators=(",", ":"))
    return rules


def context_prices_for_rule(rule: Any) -> list[dict[str, Any]]:
    if rule.context_tiers_json not in (None, "[]") or rule.long_threshold_tokens is None:
        return json.loads(rule.context_tiers_json or "[]")
    # Old snapshots are converted to explicit prices for historical compatibility.
    # Fresh CPAMP imports and the editor never create multiplier rules.
    result = []
    for tier in ("default", "priority", "flex"):
        rates = {}
        for field in FIELDS:
            rate = getattr(rule, f"{field}_nano_per_token")
            if tier != "default":
                override = getattr(rule, f"{tier}_{field}_nano_per_token")
                if override is not None:
                    rate = override
            multiplier = rule.long_output_multiplier_ppm if field == "output" else rule.long_input_multiplier_ppm
            rates[field] = (rate * multiplier + 500_000) // 1_000_000
        result.append({"service_tier": tier, "threshold_tokens": rule.long_threshold_tokens, **rates})
    return result


def select_context_price(rule: Any, input_tokens: int, tier: str,
                         prices: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    tier = "priority" if tier == "fast" else tier
    tier = tier if tier in {"priority", "flex"} else "default"
    if prices is None:
        prices = context_prices_for_rule(rule)
    if tier != "default" and not any(price["service_tier"] == tier for price in prices) and not any(
        getattr(rule, f"{tier}_{field}_nano_per_token") is not None for field in FIELDS
    ):
        tier = "default"
    matches = [price for price in prices
               if price["service_tier"] == tier and input_tokens > price["threshold_tokens"]]
    return max(matches, key=lambda price: price["threshold_tokens"]) if matches else None
