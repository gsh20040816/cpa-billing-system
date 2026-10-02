import json
import sqlite3

import pytest
from sqlalchemy import select

from cpa_billing.context_prices import cpamp_rules, normalize_context_prices, select_context_price
from cpa_billing.models import ModelPriceRule, RatedEvent
from test_services import insert_event


def import_prices(service, settings, raw, name="context-bands"):
    with sqlite3.connect(settings.cpamp_database_path) as db:
        db.execute("update model_prices set source='models.dev', raw_json=? where model='gpt-test'", (json.dumps(raw),))
    return service.import_cpamp_prices(name)


@pytest.mark.parametrize("tokens,expected", [(100, [1000, 100, 1250, 6000]), (101, [7000, 900, 0, 12000]),
                                           (200, [7000, 900, 0, 12000]), (201, [0, 100, 1250, 31000])])
def test_multiple_context_bands_use_exact_prices_for_the_whole_request(service, settings, tokens, expected):
    raw = {"cost": {"tiers": [
        {"tier": {"type": "context", "size": 200}, "input": 0, "output": 31},
        {"tier": {"type": "context", "size": 100}, "input": 7, "output": 12, "cache_read": .9, "cache_write": 0},
    ]}}
    version = import_prices(service, settings, raw)
    insert_event(settings, "context-key", 1000, input_tokens=tokens, cached_tokens=0,
                 cache_read_tokens=10, cache_creation_tokens=20, output_tokens=5)
    service.sync_cpamp()
    assert service.rate_events() == 1
    with service.db.session() as db:
        rule = db.get(ModelPriceRule, (version, "gpt-test"))
        assert rule.long_threshold_tokens is None
        rated = db.scalar(select(RatedEvent))
        assert json.loads(rated.calculation_json)["rates"] == expected
        assert rated.rated_weight_nano_usd == (tokens - 30) * expected[0] + 10 * expected[1] + 20 * expected[2] + 5 * expected[3]
        assert rated.long_context_applied is (tokens > 100)


def test_service_tier_does_not_invent_context_multipliers(service, settings):
    raw = {"cost": {"tiers": [{"tier": {"type": "context", "size": 100}, "input": 7, "output": 13}]},
           "experimental": {"modes": {"fast": {"cost": {"input": 3, "output": 9}}}}}
    version = import_prices(service, settings, raw)
    with service.db.session() as db:
        rule = db.get(ModelPriceRule, (version, "gpt-test"))
        assert select_context_price(rule, 101, "priority") is None
    insert_event(settings, "priority-key", 1000, tier="priority", input_tokens=101, cached_tokens=0,
                 cache_read_tokens=0, cache_creation_tokens=0, output_tokens=5)
    service.sync_cpamp()
    service.rate_events()
    with service.db.session() as db:
        rated = db.scalar(select(RatedEvent))
        assert json.loads(rated.calculation_json)["rates"] == [3000, 100, 1250, 9000]


def test_zero_base_and_independent_flex_cache_prices(service, settings):
    with sqlite3.connect(settings.cpamp_database_path) as db:
        db.execute("update model_prices set prompt_per_1m=0 where model='gpt-test'")
    raw = {"cost": {"tiers": [{"tier": {"type": "context", "size": 100}, "input": 2}]},
           "experimental": {"modes": {"flex": {"provider": {"body": {"service_tier": "flex"}},
               "cost": {"input": 0, "output": 0, "cache_read": .7, "cache_write": .8,
                        "tiers": [{"tier": {"type": "context", "size": 100}, "cache_read": .9, "cache_write": 1.1}]}}}}}
    version = import_prices(service, settings, raw)
    with service.db.session() as db:
        rule = db.get(ModelPriceRule, (version, "gpt-test"))
        assert select_context_price(rule, 101, "default")["input"] == 2000
        assert select_context_price(rule, 101, "flex")["cache_read"] == 900
        assert rule.flex_cache_creation_nano_per_token == 800


@pytest.mark.parametrize("threshold", [0, -1, 1.5, True])
def test_bad_thresholds_are_rejected(threshold):
    with pytest.raises(ValueError):
        normalize_context_prices([{"threshold_tokens": threshold, "input": 1, "output": 1, "cache_read": 0, "cache_creation": 0}])


def test_duplicate_thresholds_rejected_without_discarding_service_tiers():
    band = {"threshold_tokens": 100, "input": 1, "output": 1, "cache_read": 0, "cache_creation": 0}
    with pytest.raises(ValueError):
        normalize_context_prices([band, band])
    assert len(normalize_context_prices([band, {**band, "service_tier": "priority"}])) == 2


def test_priority_without_overrides_uses_default_context_band(service, settings):
    raw = {"cost": {"tiers": [{"tier": {"type": "context", "size": 100}, "input": 7}]}}
    version = import_prices(service, settings, raw)
    with service.db.session() as db:
        rule = db.get(ModelPriceRule, (version, "gpt-test"))
        assert select_context_price(rule, 101, "priority")["input"] == 7000
        assert select_context_price(rule, 100, "priority") is None


def test_context_migration_preserves_legacy_prices_and_is_idempotent():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    path = Path(__file__).parents[1] / "migrations/versions/0015_explicit_context_prices.py"
    spec = importlib.util.spec_from_file_location("context_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE model_price_rules (model TEXT, long_threshold_tokens INTEGER, long_input_multiplier_ppm INTEGER)"))
        db.execute(text("INSERT INTO model_price_rules VALUES ('legacy', 200000, 2000000)"))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            migration.upgrade()
        row = db.execute(text("SELECT * FROM model_price_rules")).mappings().one()
        assert row["long_threshold_tokens"] == 200000
        assert row["long_input_multiplier_ppm"] == 2000000
        assert row["context_tiers_json"] is None
        assert row["flex_cache_read_nano_per_token"] is None


def test_explicit_bands_survive_republish_and_change_detection(service, settings):
    raw = {"cost": {"tiers": [{"tier": {"type": "context", "size": 100}, "input": 7}]}}
    version = import_prices(service, settings, raw)
    result = service.republish_active_pricing("preserve context prices")
    assert result["changed_models"] == []
    with service.db.session() as db:
        from cpa_billing.models import PricingVersion
        active = db.scalar(select(PricingVersion).where(PricingVersion.status == "active"))
        original = db.get(ModelPriceRule, (version, "gpt-test"))
        copied = db.get(ModelPriceRule, (active.id, "gpt-test"))
        assert copied.context_tiers_json == original.context_tiers_json
        assert service._changed_price_models([original], [copied]) == []
        prices = json.loads(copied.context_tiers_json)
        prices[0]["input"] += 1
        copied.context_tiers_json = json.dumps(prices)
        assert service._changed_price_models([original], [copied]) == ["gpt-test"]
