from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from cpa_billing.database import Database
from cpa_billing.domain import NANO_USD
from cpa_billing.models import (
    APIKey,
    Adjustment,
    AuditLog,
    BillingCycle,
    CyclePoolCost,
    DeadLetter,
    GradientRule,
    GroupMembership,
    KeyOwnershipPeriod,
    ManualUsageAdjustment,
    MeteredKeyCharge,
    PricingVersion,
    RatedEvent,
    RawUsageEvent,
    ResourcePool,
    Statement,
    StatementLine,
    SyncCheckpoint,
    TelegramUser,
)
from cpa_billing.security import cpamp_key_hash
from cpa_billing.services import BillingError, BillingService


def insert_event(settings, key_hash: str, timestamp_ms: int, *, event_hash: str = "e1", input_tokens: int = 1000,
                 cached_tokens: int = 100, output_tokens: int = 100, tier: str = "default",
                 model: str = "gpt-test", failed: bool = False, fail_status_code: int | None = None,
                 latency_ms: int = 100, ttft_ms: int = 10, cache_tokens: int = 0,
                 cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
                 reasoning_effort: str | None = None, account_snapshot: str = "account", auth_index: str = "auth") -> None:
    db = sqlite3.connect(settings.cpamp_database_path)
    db.execute("""insert into usage_events(event_hash,request_id,timestamp_ms,timestamp,provider,executor_type,model,
               requested_model,resolved_model,reasoning_effort,service_tier,api_key_hash,source_hash,source,account_snapshot,auth_index,
               input_tokens,output_tokens,reasoning_tokens,cached_tokens,cache_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,
               failed,fail_status_code,latency_ms,ttft_ms) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (event_hash, f"request-{event_hash}", timestamp_ms, "2026-07-04T00:00:00Z", "codex", "CodexExecutor", model, model, model, reasoning_effort, tier,
                key_hash, "source", "masked", account_snapshot, auth_index, input_tokens, output_tokens, 40,
                cached_tokens, cache_tokens, cache_read_tokens, cache_creation_tokens,
                input_tokens + output_tokens, int(failed), fail_status_code, latency_ms, ttft_ms))
    db.commit(); db.close()


def create_owner(service, raw_key: str, user_id: int, start_ms: int) -> None:
    with service.db.session() as session:
        session.add(TelegramUser(telegram_user_id=user_id, username=f"u{user_id}", registered_at_ms=start_ms, last_seen_at_ms=start_ms))
        session.flush()
        key = APIKey(cpamp_hash=cpamp_key_hash(raw_key), login_fingerprint="f" + str(user_id), masked_value="masked", status="active",
                     current_owner_id=user_id, created_at_ms=start_ms)
        session.add(key); session.flush()
        session.add(KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=user_id, valid_from_ms=start_ms, source="test", created_at_ms=start_ms))


def test_sync_is_incremental_and_idempotent(service, settings) -> None:
    insert_event(settings, "hash", 1000)
    assert service.sync_cpamp() == 1
    assert service.sync_cpamp() == 0
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(RawUsageEvent)) == 1


def test_sync_retries_unresolved_dead_letters(service, settings) -> None:
    insert_event(settings, "first", 1000, event_hash="first")
    assert service.sync_cpamp() == 1
    insert_event(settings, "second", 2000, event_hash="second")
    with service.db.session() as session:
        checkpoint = session.scalar(select(SyncCheckpoint))
        session.add(DeadLetter(
            source_id=checkpoint.source_id,
            source_event_id=2,
            error="transient database lock",
            payload_json="{}",
            created_at_ms=1,
        ))
        checkpoint.last_event_id = 2
        checkpoint.last_event_at_ms = 2000

    assert service.sync_cpamp() == 1
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(RawUsageEvent)) == 2
        dead_letter = session.scalar(select(DeadLetter))
        assert dead_letter.resolved_at_ms is not None


def test_concurrent_rating_is_idempotent(service, settings) -> None:
    insert_event(settings, "hash", 1000)
    service.sync_cpamp()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.rate_events(), range(2)))
    assert sum(results) == 1
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(RatedEvent)) == 1


def test_rating_uses_cached_subset_and_reasoning_not_added(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); assert service.rate_events() == 1
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        assert rated.telegram_user_id == 2
        assert rated.rated_weight_nano_usd == 900 * 1000 + 100 * 100 + 100 * 6000


def test_explicit_cache_read_is_not_charged_as_uncached_input(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        event_hash="explicit-cache-read",
        input_tokens=1000,
        cached_tokens=0,
        cache_read_tokens=900,
        output_tokens=100,
    )
    service.sync_cpamp(); assert service.rate_events() == 1
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        detail = json.loads(rated.calculation_json)
        assert detail["uncached"] == 100
        assert rated.rated_weight_nano_usd == 100 * 1000 + 900 * 100 + 100 * 6000


def test_explicit_cache_creation_is_not_charged_as_uncached_input(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        event_hash="explicit-cache-creation",
        input_tokens=1000,
        cached_tokens=0,
        cache_creation_tokens=200,
        output_tokens=100,
    )
    service.sync_cpamp(); assert service.rate_events() == 1
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        detail = json.loads(rated.calculation_json)
        assert detail["uncached"] == 800
        assert rated.rated_weight_nano_usd == 800 * 1000 + 200 * 1250 + 100 * 6000


def test_priority_and_long_context_combine(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        input_tokens=300000,
        cached_tokens=0,
        output_tokens=10,
        tier="fast",
        model="gpt-5.6-luna",
    )
    service.sync_cpamp(); service.rate_events()
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        detail = json.loads(rated.calculation_json)
        assert rated.long_context_applied is True
        assert rated.service_tier == "priority"
        assert detail["rates"][0] == 4000


def test_long_context_uses_the_active_price_rule_and_can_be_republished(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        input_tokens=300000,
        cached_tokens=0,
        output_tokens=10,
        tier="fast",
        model="gpt-5.6-luna",
    )
    service.sync_cpamp()
    assert service.rate_events() == 1
    with service.db.session() as session:
        first = session.scalar(select(RatedEvent))
        assert first.long_context_applied is True

    updated = service.update_pricing_rule(
        "gpt-5.6-luna",
        {
            "input_nano_per_token": 1_000,
            "output_nano_per_token": 6_000,
            "cache_read_nano_per_token": 100,
            "cache_creation_nano_per_token": 1_250,
            "long_threshold_tokens": None,
            "long_input_multiplier_ppm": 2_000_000,
            "long_output_multiplier_ppm": 1_500_000,
        },
        "without-long-context",
        "remove long context multiplier",
        "test",
        "test",
    )
    assert updated["rating_status"] == "queued"
    assert service.rate_events() == 1
    with service.db.session() as session:
        active = session.scalar(select(PricingVersion).where(PricingVersion.status == "active"))
        rated = session.scalar(select(RatedEvent).where(RatedEvent.pricing_version_id == active.id))
        detail = json.loads(rated.calculation_json)
        assert rated.long_context_applied is False
        assert detail["rates"] == [2000, 200, 2500, 12000]

    republished = service.republish_active_pricing("rebuild current pricing")
    assert republished["rating_status"] == "queued"
    assert service.rate_events() == 1
    with service.db.session() as session:
        active = session.scalar(select(PricingVersion).where(PricingVersion.status == "active"))
        rated = session.scalar(select(RatedEvent).where(RatedEvent.pricing_version_id == active.id))
        assert rated.long_context_applied is False


def test_cycle_allocation_groups_by_user_and_preserves_cost(service, settings) -> None:
    create_owner(service, "key1", 2, 0); create_owner(service, "key2", 3, 0)
    insert_event(settings, cpamp_key_hash("key1"), 1000, event_hash="a", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    insert_event(settings, cpamp_key_hash("key2"), 1001, event_hash="b", input_tokens=2_000_000, cached_tokens=0, output_tokens=0)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 109000)
    statements = service.preview_cycle("cycle")
    assert sum(row.amount_cents for row in statements) == 109000
    assert {row.telegram_user_id for row in statements} == {2, 3}


def test_cycle_with_cost_and_no_billable_user_is_blocked(service, settings) -> None:
    insert_event(settings, "unowned", 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    with pytest.raises(BillingError, match="no billable Telegram usage"):
        service.preview_cycle("cycle")


def test_adjustment_only_user_gets_statement(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    with service.db.session() as session:
        cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == "cycle"))
        session.add(Adjustment(cycle_id=cycle.id, telegram_user_id=2, amount_cents=250,
                               reason="manual credit", operator_user_id=None, created_at_ms=1))
    statements = service.preview_cycle("cycle")
    assert len(statements) == 1
    assert statements[0].amount_cents == 250
    assert statements[0].adjustment_cents == 250


def test_manual_usage_is_applied_before_gradient_without_creating_requests(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    with service.db.session() as session:
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))

    adjustment_id = service.add_manual_usage_adjustment(
        "manual-cycle",
        pool_id,
        2,
        400 * NANO_USD,
        "补录线下消耗",
        None,
        operator_type="web-admin",
    )
    statements = service.preview_cycle("manual-cycle")
    assert len(statements) == 1
    assert statements[0].actual_weight_nano_usd == 400 * NANO_USD
    assert statements[0].billed_weight_nano_usd == 390 * NANO_USD
    assert statements[0].amount_cents == 10000

    dashboard = service.dashboard("manual-cycle")
    row = next(item for item in dashboard["rows"] if item["telegram_user_id"] == 2)
    assert row["requests"] == 0
    assert row["tokens"] == 0
    assert row["request_actual"] == "0.0000"
    assert row["manual_actual"] == "400.0000"
    assert row["actual"] == "400.0000"
    assert dashboard["totals"]["manual_actual"] == "400.0000"
    assert dashboard["totals"]["actual"] == "400.0000"
    assert service.request_history(2)["pagination"]["total"] == 0
    with service.db.session() as session:
        assert session.get(ManualUsageAdjustment, adjustment_id).amount_nano_usd == 400 * NANO_USD
        assert session.scalar(select(func.count()).select_from(RawUsageEvent)) == 0


def test_manual_usage_only_affects_its_configured_resource_pool(service) -> None:
    create_owner(service, "key-two", 2, 0)
    create_owner(service, "key-three", 3, 0)
    with service.db.session() as session:
        default_pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
        second_pool = ResourcePool(name="second-pool", active=True, created_at_ms=1)
        session.add(second_pool)
        session.flush()
        default_pool_id, second_pool_id = default_pool.id, second_pool.id
    service.create_cycle(
        "multi-pool",
        "1970-01-01T08:00",
        "1970-01-02T08:00",
        0,
        pool_costs=[
            {"pool_id": default_pool_id, "fixed_cost_cents": 1000},
            {"pool_id": second_pool_id, "fixed_cost_cents": 2000},
        ],
    )
    service.add_manual_usage_adjustment("multi-pool", default_pool_id, 2, NANO_USD, "pool one", None)
    service.add_manual_usage_adjustment("multi-pool", second_pool_id, 3, 2 * NANO_USD, "pool two", None)
    statements = service.preview_cycle("multi-pool")
    assert {row.telegram_user_id: row.amount_cents for row in statements} == {2: 1000, 3: 2000}
    with service.db.session() as session:
        lines = list(session.scalars(select(StatementLine).join(Statement).where(
            Statement.cycle_id == session.scalar(select(BillingCycle.id).where(BillingCycle.name == "multi-pool"))
        )))
    assert {(line.pool_id, line.actual_weight_nano_usd) for line in lines} == {
        (default_pool_id, NANO_USD),
        (second_pool_id, 2 * NANO_USD),
    }


def test_manual_usage_reversals_preserve_balance_and_cannot_overdraw(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-reversal", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    with service.db.session() as session:
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
    service.add_manual_usage_adjustment("manual-reversal", pool_id, 2, 1_234_567_890, "initial", None)
    service.add_manual_usage_adjustment("manual-reversal", pool_id, 2, -234_567_890, "reverse error", None)
    statement = service.preview_cycle("manual-reversal")[0]
    assert statement.actual_weight_nano_usd == NANO_USD
    with service.db.session() as session:
        rows = list(session.scalars(select(ManualUsageAdjustment).order_by(ManualUsageAdjustment.id)))
    assert [row.amount_nano_usd for row in rows] == [1_234_567_890, -234_567_890]
    with pytest.raises(BillingError, match="冲销金额不能超过"):
        service.add_manual_usage_adjustment("manual-reversal", pool_id, 2, -NANO_USD - 1, "overdraw", None)
    with pytest.raises(BillingError, match="不能为零"):
        service.add_manual_usage_adjustment("manual-reversal", pool_id, 2, 0, "zero", None)


def test_manual_usage_update_moves_all_business_fields_and_recalculates_cycles(service) -> None:
    create_owner(service, "key-two", 2, 0)
    create_owner(service, "key-three", 3, 0)
    with service.db.session() as session:
        default_pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
        target_pool = ResourcePool(name="target-pool", active=True, created_at_ms=1)
        session.add(target_pool)
        session.flush()
        target_pool_id = target_pool.id
    service.create_cycle("manual-source", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    service.create_cycle(
        "manual-target",
        "1970-01-02T08:00",
        "1970-01-03T08:00",
        0,
        pool_costs=[{"pool_id": target_pool_id, "fixed_cost_cents": 2000}],
    )
    adjustment_id = service.add_manual_usage_adjustment(
        "manual-source", default_pool_id, 2, 400 * NANO_USD, "source usage", None,
    )
    service.preview_cycle("manual-source")

    service.update_manual_usage_adjustment(
        adjustment_id,
        "manual-target",
        target_pool_id,
        3,
        50 * NANO_USD,
        "moved usage",
        None,
        operator_type="web-admin",
    )

    source = service.dashboard("manual-source")
    target = service.dashboard("manual-target")
    assert next(row for row in source["rows"] if row["telegram_user_id"] == 2)["manual_actual"] == "0.0000"
    target_row = next(row for row in target["rows"] if row["telegram_user_id"] == 3)
    assert target_row["manual_actual"] == "50.0000"
    assert target_row["amount"] == "20.00"
    with service.db.session() as session:
        row = session.get(ManualUsageAdjustment, adjustment_id)
        source_cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == "manual-source"))
        target_cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == "manual-target"))
        audit = session.scalar(select(AuditLog).where(AuditLog.operation == "manual-usage.update"))
        assert (row.cycle_id, row.pool_id, row.telegram_user_id) == (target_cycle.id, target_pool_id, 3)
        assert row.amount_nano_usd == 50 * NANO_USD
        assert row.reason == "moved usage"
        assert row.updated_at_ms is not None
        assert source_cycle.status == "open"
        assert session.scalar(select(func.count()).select_from(Statement).where(Statement.cycle_id == source_cycle.id)) == 0
        assert json.loads(audit.before_json)["cycle"] == "manual-source"
        assert json.loads(audit.after_json)["cycle"] == "manual-target"
    snapshot = service.admin_snapshot()["manual_usage_adjustments"][0]
    assert snapshot["editable"] is True
    assert snapshot["updated_at"] is not None


def test_manual_usage_update_preserves_group_balances_and_closed_cycles(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-edit", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    service.create_cycle("manual-edit-target", "1970-01-02T08:00", "1970-01-03T08:00", 0)
    with service.db.session() as session:
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
    positive_id = service.add_manual_usage_adjustment("manual-edit", pool_id, 2, 1_200_000_000, "positive", None)
    negative_id = service.add_manual_usage_adjustment("manual-edit", pool_id, 2, -200_000_000, "reversal", None)

    with pytest.raises(BillingError, match="目标补录余额为负数"):
        service.update_manual_usage_adjustment(positive_id, "manual-edit", pool_id, 2, 100_000_000, "too small", None)
    with pytest.raises(BillingError, match="已有后续冲销"):
        service.update_manual_usage_adjustment(
            positive_id, "manual-edit-target", pool_id, 2, 1_200_000_000, "move positive", None,
        )
    with pytest.raises(BillingError, match="目标补录余额为负数"):
        service.update_manual_usage_adjustment(negative_id, "manual-edit", pool_id, 2, -1_300_000_000, "too negative", None)
    with pytest.raises(BillingError, match="没有变化"):
        service.update_manual_usage_adjustment(positive_id, "manual-edit", pool_id, 2, 1_200_000_000, "positive", None)

    service.close_cycle("manual-edit", 1, False)
    with pytest.raises(BillingError, match="已关闭账期"):
        service.update_manual_usage_adjustment(positive_id, "manual-edit", pool_id, 2, NANO_USD, "late edit", None)


def test_manual_usage_update_rejects_closed_target_cycle(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-open-source", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    service.create_cycle("manual-closed-target", "1970-01-02T08:00", "1970-01-03T08:00", 0)
    with service.db.session() as session:
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
    adjustment_id = service.add_manual_usage_adjustment(
        "manual-open-source", pool_id, 2, NANO_USD, "source", None,
    )
    service.close_cycle("manual-closed-target", 1, False)
    with pytest.raises(BillingError, match="已经关闭"):
        service.update_manual_usage_adjustment(
            adjustment_id, "manual-closed-target", pool_id, 2, NANO_USD, "closed target", None,
        )


def test_admin_snapshot_lists_all_manual_usage_records(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-list", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    with service.db.session() as session:
        cycle_id = session.scalar(select(BillingCycle.id).where(BillingCycle.name == "manual-list"))
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
        session.add_all([
            ManualUsageAdjustment(
                cycle_id=cycle_id,
                pool_id=pool_id,
                telegram_user_id=2,
                amount_nano_usd=NANO_USD,
                reason=f"record {index}",
                operator_user_id=None,
                created_at_ms=index + 1,
            )
            for index in range(101)
        ])
    assert len(service.admin_snapshot()["manual_usage_adjustments"]) == 101


def test_manual_usage_rejects_invalid_subject_pool_and_closed_cycle(service) -> None:
    create_owner(service, "key", 2, 0)
    service.create_cycle("manual-closed", "1970-01-01T08:00", "1970-01-02T08:00", 0)
    with service.db.session() as session:
        pool_id = session.scalar(select(ResourcePool.id).where(ResourcePool.name == "default-cpa"))
        unregistered = TelegramUser(telegram_user_id=9, username="u9", registered_at_ms=None, last_seen_at_ms=1)
        unconfigured_pool = ResourcePool(name="unconfigured-pool", active=True, created_at_ms=1)
        session.add_all([unregistered, unconfigured_pool])
        session.flush()
        unconfigured_pool_id = unconfigured_pool.id
    with pytest.raises(BillingError, match="尚未注册"):
        service.add_manual_usage_adjustment("manual-closed", pool_id, 9, NANO_USD, "invalid user", None)
    with pytest.raises(BillingError, match="未配置到该账期"):
        service.add_manual_usage_adjustment("manual-closed", unconfigured_pool_id, 2, NANO_USD, "invalid pool", None)
    service.close_cycle("manual-closed", 1, False)
    with pytest.raises(BillingError, match="已经关闭"):
        service.add_manual_usage_adjustment("manual-closed", pool_id, 2, NANO_USD, "too late", None)


def test_closed_cycle_is_immutable(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    service.close_cycle("cycle", 1, False)
    first = service.preview_cycle("cycle")
    assert first[0].final is True


def test_dashboard_and_reconciliation_use_cycle_pricing_version(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp()
    assert service.rate_events() == 1
    service.import_cpamp_prices("second-price-version")
    assert service.rate_events() == 1
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)

    dashboard = service.dashboard("cycle")
    assert dashboard["totals"]["requests"] == 1
    assert next(row for row in dashboard["rows"] if row["telegram_user_id"] == 2)["requests"] == 1
    assert service.rankings()[0]["requests"] == 1
    assert service.reconciliation()["rated_events"] == 1


def test_user_session_is_invalidated_when_login_key_is_revoked(service) -> None:
    create_owner(service, "key", 2, 0)
    with service.db.session() as session:
        key = session.scalar(select(APIKey).where(APIKey.current_owner_id == 2))
        key_id = key.id
    token, _ = service.create_session(2, key_id)
    assert service.get_session(token) is not None

    with service.db.session() as session:
        session.get(APIKey, key_id).status = "revoked"
    assert service.get_session(token) is None


def test_admin_token_rotation_invalidates_existing_admin_sessions(service, settings) -> None:
    token, _ = service.create_admin_session()
    assert service.get_admin_session(token) is not None
    rotated = BillingService(replace(settings, admin_token="rotated-admin-token"), Database(settings.database_path))
    assert rotated.get_admin_session(token) is None


def test_request_history_uses_historical_ownership_and_keeps_unpriced_events(service, settings) -> None:
    create_owner(service, "key-two", 2, 0)
    create_owner(service, "key-three", 3, 0)
    insert_event(settings, cpamp_key_hash("key-two"), 1000, event_hash="owned", latency_ms=250, ttft_ms=25)
    insert_event(
        settings,
        cpamp_key_hash("key-two"),
        2000,
        event_hash="unpriced",
        model="unknown-model",
        failed=True,
        fail_status_code=429,
        latency_ms=500,
        ttft_ms=50,
    )
    insert_event(settings, cpamp_key_hash("key-three"), 3000, event_hash="other")
    service.sync_cpamp()
    assert service.rate_events() == 2

    history = service.request_history(2, page_size=10)
    assert history["pagination"]["total"] == 2
    assert {item["request_id"] for item in history["items"]} == {"request-owned", "request-unpriced"}
    assert history["summary"]["unpriced"] == 1
    unpriced = next(item for item in history["items"] if item["pricing_status"] == "unpriced")
    assert unpriced["cost"] is None
    assert unpriced["status_code"] == 429

    failed = service.request_history(2, status="failed", failure_code=429, min_latency=400)
    assert failed["pagination"]["total"] == 1
    assert failed["items"][0]["request_id"] == "request-unpriced"
    assert service.request_history(3)["pagination"]["total"] == 1
    ranking = service.ranking_snapshot("all")
    user_two = next(item for item in ranking["rows"] if item["telegram_user_id"] == 2)
    assert user_two["requests"] == 2
    assert user_two["tokens"] == 2200
    bot_ranking = next(item for item in service.rankings() if item["telegram_user_id"] == 2)
    assert bot_ranking["requests"] == 2


def test_request_history_separates_failed_200_from_other_failures(service, settings) -> None:
    create_owner(service, "status-key", 2, 0)
    insert_event(settings, cpamp_key_hash("status-key"), 1000, event_hash="status-success")
    insert_event(
        settings,
        cpamp_key_hash("status-key"),
        2000,
        event_hash="status-failed-200",
        failed=True,
        fail_status_code=200,
    )
    insert_event(
        settings,
        cpamp_key_hash("status-key"),
        3000,
        event_hash="status-failed-429",
        failed=True,
        fail_status_code=429,
    )
    insert_event(
        settings,
        cpamp_key_hash("status-key"),
        4000,
        event_hash="status-failed-unknown",
        failed=True,
    )
    service.sync_cpamp()
    service.rate_events()

    assert service.request_history(2, status="success")["pagination"]["total"] == 1
    assert service.request_history(2, status="failed_200")["pagination"]["total"] == 1
    assert service.request_history(2, status="failed_other")["pagination"]["total"] == 2
    assert service.request_history(2, status="failed")["pagination"]["total"] == 3


def test_admin_request_history_includes_all_users_and_unowned_events(service, settings) -> None:
    create_owner(service, "key-two", 2, 0)
    create_owner(service, "key-three", 3, 0)
    insert_event(settings, cpamp_key_hash("key-two"), 1000, event_hash="admin-owned-two")
    insert_event(settings, cpamp_key_hash("key-three"), 2000, event_hash="admin-owned-three")
    insert_event(settings, cpamp_key_hash("missing-key"), 3000, event_hash="admin-unowned")
    service.sync_cpamp()
    assert service.rate_events() == 3

    own = service.request_history(2)
    assert own["pagination"]["total"] == 1
    assert len(service.request_filter_options(2)["keys"]) == 1

    history = service.request_history(None, all_users=True, sort="time_asc")
    assert history["pagination"]["total"] == 3
    assert [item["owner"]["telegram_user_id"] if item["owner"] else None for item in history["items"]] == [2, 3, None]
    assert history["items"][0]["owner"]["name"] == "@u2"
    assert history["items"][2]["key"]["id"] is None
    assert history["items"][2]["key"]["masked"].startswith("key:")

    options = service.request_filter_options(None, all_users=True)
    assert len(options["keys"]) == 2
    assert options["models"] == ["gpt-test"]


def test_usage_ranges_share_today_cycle_and_integer_hours_semantics(service) -> None:
    create_owner(service, "range-key", 2, 0)
    current = int(time.time() * 1000)
    zone = ZoneInfo(service.settings.timezone)
    cycle_start = datetime.fromtimestamp((current - 3_600_000) / 1000, zone).isoformat()
    cycle_end = datetime.fromtimestamp((current + 3_600_000) / 1000, zone).isoformat()
    service.create_cycle("range-cycle", cycle_start, cycle_end, 0)

    history = service.request_history(2, range_name="custom", custom_hours=2)
    assert history["pagination"]["total"] == 0
    ranking = service.ranking_snapshot("cycle", cycle_name="range-cycle")
    assert ranking["range"]["cycle"] == "range-cycle"

    with service.db.session() as session:
        start, end, selected = service._resolve_time_range(session, "24h")
        assert selected is None
        assert end - start == 24 * 60 * 60 * 1000
        today_start, today_end, _ = service._resolve_time_range(session, "today")
        yesterday_start, yesterday_end, _ = service._resolve_time_range(session, "yesterday")
        assert today_start < today_end
        assert yesterday_start < yesterday_end == today_start
        with pytest.raises(BillingError, match="正整数小时"):
            service._resolve_time_range(session, "custom", custom_hours=1.5)


def test_web_key_actions_execute_directly_and_invalidate_target_sessions(service, settings, monkeypatch) -> None:
    raw_keys = ["current-key", "target-key"]

    def list_keys() -> list[str]:
        return list(raw_keys)

    def add_key(raw: str) -> None:
        raw_keys.append(raw)

    def remove_key_hash(key_hash: str) -> str | None:
        for raw in list(raw_keys):
            if cpamp_key_hash(raw) == key_hash:
                raw_keys.remove(raw)
                return raw
        return None

    def replace_key_hash(key_hash: str, new_raw: str) -> str:
        for index, raw in enumerate(raw_keys):
            if cpamp_key_hash(raw) == key_hash:
                raw_keys[index] = new_raw
                return raw
        raise BillingError("missing")

    monkeypatch.setattr(service.cpa, "list_keys", list_keys)
    monkeypatch.setattr(service.cpa, "add_key", add_key)
    monkeypatch.setattr(service.cpa, "remove_key_hash", remove_key_hash)
    monkeypatch.setattr(service.cpa, "replace_key_hash", replace_key_hash)

    with service.db.session() as session:
        session.add(TelegramUser(telegram_user_id=2, username="u2", registered_at_ms=1, last_seen_at_ms=1))
        session.flush()
        current = service._insert_key(session, "current-key", 2, "test")
        target = service._insert_key(session, "target-key", 2, "test")
        current_id, target_id = current.id, target.id

    token, _ = service.create_session(2, target_id)
    result = service.execute_web_key_action(2, "current-key", "reset", target_id)
    assert result["new_api_key"] in raw_keys
    assert "target-key" not in raw_keys
    assert service.get_session(token) is None
    with service.db.session() as session:
        assert session.get(APIKey, target_id).status == "revoked"
        assert session.get(APIKey, result["new_key_id"]).status == "active"

    add_result = service.execute_web_key_action(2, "current-key", "add", None)
    assert add_result["new_api_key"] in raw_keys
    current_token, _ = service.create_session(2, current_id)
    revoke_result = service.execute_web_key_action(2, add_result["new_api_key"], "revoke", current_id)
    assert revoke_result["new_api_key"] is None
    assert service.get_session(current_token) is None


def test_confirmation_token_is_claimed_before_external_key_operation(service, monkeypatch) -> None:
    raw_keys = ["current-key"]
    monkeypatch.setattr(service.cpa, "list_keys", lambda: list(raw_keys))
    monkeypatch.setattr(service.cpa, "add_key", lambda raw: raw_keys.append(raw))
    monkeypatch.setattr(service.cpa, "remove_key_hash", lambda key_hash: None)
    with service.db.session() as session:
        session.add(TelegramUser(telegram_user_id=2, username="u2", registered_at_ms=1, last_seen_at_ms=1))
        session.flush()
        service._insert_key(session, "current-key", 2, "test")
    token = service.request_key_action(2, "current-key", "add", None)

    entered = threading.Event()
    release = threading.Event()
    original = service._execute_key_action
    calls = []

    def slow_execute(*args, **kwargs):
        calls.append(1)
        entered.set()
        release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_key_action", slow_execute)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.confirm_key_action, 2, token)
        assert entered.wait(timeout=5)
        second = executor.submit(service.confirm_key_action, 2, token)
        with pytest.raises(BillingError, match="invalid or expired"):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).startswith("sk-cpa-")
    assert len(calls) == 1


def test_cpa_accounts_are_sanitized_and_refresh_uses_public_account_ids(service, monkeypatch) -> None:
    current = int(time.time() * 1000)
    reset_at = current + 3_600_000
    files = [{
            "id": "7",
            "name": "secret@example.com",
            "label": "Shared Pro",
            "auth_index": "raw-auth-index",
            "path": "/root/private/secret.json",
            "type": "codex",
            "provider": "codex",
            "account_type": "oauth",
            "id_token": {"plan_type": "pro"},
            "disabled": False,
        }]
    quota = {
        "plan_type": "pro",
        "rate_limit": {"primary_window": {
            "used_percent": 42,
            "allowed": True,
            "limit_reached": False,
            "limit_window_seconds": 18000,
            "reset_at": reset_at,
        }},
        "additional_rate_limits": [{
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {"primary_window": {
                "used_percent": 7,
                "limit_window_seconds": 18000,
                "reset_at": reset_at,
            }},
        }],
    }
    monkeypatch.setattr(service.cpa, "auth_files", lambda: files)
    monkeypatch.setattr(service.cpa, "api_call", lambda *args, **kwargs: {
        "status_code": 200,
        "body": json.dumps(quota),
    })
    insert_event(
        service.settings,
        "account-key",
        current - 4 * 3_600_000 - 1,
        event_hash="account-outside-window",
        auth_index="raw-auth-index",
        account_snapshot="Shared Pro",
    )
    insert_event(
        service.settings,
        "account-key",
        current - 1000,
        event_hash="account-local",
        auth_index="raw-auth-index",
        account_snapshot="Shared Pro",
    )
    insert_event(
        service.settings,
        "account-key",
        current - 500,
        event_hash="account-other-model",
        model="gpt-5.6-luna",
        auth_index="raw-auth-index",
        account_snapshot="Shared Pro",
    )
    insert_event(
        service.settings,
        "account-key",
        current - 250,
        event_hash="account-spark-model",
        model="gpt-5.3-codex-spark",
        auth_index="raw-auth-index",
        account_snapshot="Shared Pro",
    )
    service.sync_cpamp()
    service.rate_events()
    snapshot = service.accounts_snapshot()
    serialized = json.dumps(snapshot)
    assert "raw-auth-index" not in serialized
    assert "secret.json" not in serialized
    assert "/root/private" not in serialized
    assert snapshot["accounts"][0]["id"] == "7"
    assert snapshot["accounts"][0]["quota"][0]["used_percent"] == 42
    assert snapshot["accounts"][0]["usage"]["requests"] == 4
    assert snapshot["accounts"][0]["usage"]["total_tokens"] == 4400
    assert snapshot["accounts"][0]["usage"]["source"] == "billing-panel"
    primary = snapshot["accounts"][0]["quota"][0]
    assert primary["window_usage_requests"] == 2
    assert primary["window_usage_tokens"] == 2200
    assert primary["available_estimate"]["status"] == "estimated"
    assert primary["available_estimate"]["used_percent_min"] == "41.5"
    assert primary["available_estimate"]["used_percent_max"] == "42.5"
    assert primary["available_estimate"]["estimated_total_cost_lower"] is not None
    assert primary["available_estimate"]["estimated_total_cost_upper"] is not None
    assert primary["usage_filter"] == {
        "mode": "all_except_models",
        "models": ["gpt-5.3-codex-spark"],
        "display_models": ["GPT-5.3-Codex-Spark"],
    }
    assert primary["window_started_at"] == datetime.fromtimestamp(
        (current - 4 * 3_600_000) / 1000,
        ZoneInfo("Asia/Shanghai"),
    ).isoformat()
    additional = snapshot["accounts"][0]["quota"][1]
    assert additional["window_usage_requests"] == 1
    assert additional["window_usage_tokens"] == 1100
    assert additional["window_usage_cost"] == "0.0015"
    assert additional["usage_filter"] == {
        "mode": "only_model",
        "models": ["gpt-5.3-codex-spark"],
        "display_models": ["GPT-5.3-Codex-Spark"],
    }

    quota["rate_limit"]["primary_window"]["reset_at"] = current + 3_600_000 + 4 * 60_000
    jittered = service.accounts_snapshot()["accounts"][0]["quota"][0]
    assert jittered["window_started_at"] == primary["window_started_at"]

    quota["rate_limit"]["primary_window"]["reset_at"] = current + 3_600_000 + 6 * 60_000
    shifted = service.accounts_snapshot()["accounts"][0]["quota"][0]
    assert shifted["window_started_at"] == datetime.fromtimestamp(
        (current - 4 * 3_600_000 + 6 * 60_000) / 1000,
        ZoneInfo("Asia/Shanghai"),
    ).isoformat()

    refresh = service.refresh_account_quotas(["7"])
    assert refresh["tasks"] == []
    assert refresh["accepted"] == 1


def test_upstream_quota_reset_requires_three_confirmations_without_cpa_exhaustion(service, monkeypatch) -> None:
    files = [{
        "id": "codex-account",
        "auth_index": "codex-auth-index",
        "name": "Codex account",
        "type": "codex",
        "provider": "codex",
        "id_token": {"chatgpt_account_id": "chatgpt-account"},
        "status": "active",
        "status_message": "",
        "unavailable": False,
        "disabled": False,
    }]
    usage = {
        "plan_type": "pro",
        "rate_limit": {"secondary_window": {
            "used_percent": 81,
            "allowed": True,
            "limit_reached": False,
            "limit_window_seconds": 7 * 24 * 60 * 60,
            "reset_at": int(time.time() * 1000) + 86_400_000,
        }},
    }
    reset_credits = {
        "available_count": 1,
        "credits": [{
            "id": "credit-1",
            "reset_type": "codex_rate_limits",
            "status": "available",
            "expires_at": "2026-08-01T03:56:22Z",
        }],
    }
    monkeypatch.setattr(service.cpa, "auth_files", lambda: files)

    def api_call(_auth_index, _method, url, _headers=None, data=""):
        assert data == ""
        assert url == "https://chatgpt.com/backend-api/wham/usage"
        return {"status_code": 200, "body": json.dumps(usage)}

    monkeypatch.setattr(service.cpa, "api_call", api_call)
    monkeypatch.setattr(service.cpa, "codex_reset_credits", lambda *_args, **_kwargs: {
        "available_count": 1,
        "credits": [{"id": "credit-1", "status": "available", "expires_at": "2026-08-01T03:56:22Z"}],
    })
    consumed = []
    monkeypatch.setattr(service.cpa, "consume_codex_reset_credit", lambda *args, **kwargs: consumed.append((args, kwargs)) or {"code": "reset", "windows_reset": 2})

    snapshot = service.accounts_snapshot()
    account = snapshot["accounts"][0]
    assert account["reset_credits"][0]["expires_at"] == "2026-08-01T03:56:22Z"
    assert account["quota_reset_guard"]["weekly_exhausted"] is False
    assert account["quota_reset_guard"]["required_confirmations"] == 3

    with pytest.raises(BillingError, match="三次确认"):
        service.reset_account_quota("codex-account", "测试", confirmations=2)
    assert consumed == []

    result = service.reset_account_quota("codex-account", "测试上游额度重置", confirmations=3)
    assert result["status"] == "reset"
    assert result["windows_reset"] == 2
    assert len(consumed) == 1
    assert consumed[0][0] == ("codex-auth-index", "chatgpt-account")


def test_quota_available_estimate_uses_half_point_bounds(service) -> None:
    estimate = service._quota_available_estimate(42, 100 * 1_000_000_000)

    assert estimate["status"] == "estimated"
    assert estimate["estimated_total_cost_lower"] == "235.2941"
    assert estimate["estimated_total_cost_upper"] == "240.9639"
    assert estimate["available_percent_min"] == "57.5"
    assert estimate["available_percent_max"] == "58.5"
    assert estimate["available_cost_lower"] == "135.2941"
    assert estimate["available_cost_upper"] == "140.9639"

    zero_percent = service._quota_available_estimate(0, 100 * 1_000_000_000)
    assert zero_percent["status"] == "unavailable"
    assert zero_percent["reason"] == "zero_percent"

    zero_cost = service._quota_available_estimate(42, 0)
    assert zero_cost["status"] == "unavailable"
    assert zero_cost["reason"] == "zero_cost"


def test_cpa_refresh_reports_failed_quota_reads_without_tasks(service, monkeypatch) -> None:
    files = [{
            "id": "7",
            "auth_index": "raw-auth-index",
            "name": "Shared Pro",
            "type": "codex",
            "provider": "codex",
            "disabled": False,
        }]
    monkeypatch.setattr(service.cpa, "auth_files", lambda: files)
    monkeypatch.setattr(service.cpa, "api_call", lambda *args, **kwargs: {"status_code": 503, "body": "{}"})
    result = service.refresh_account_quotas(["7"])
    assert result["tasks"] == []
    assert result["rejected"] == [{"account_id": "7", "error": "上游额度接口返回 HTTP 503"}]
    assert result["accepted"] == 0


def test_membership_cache_can_require_recent_group_status(service) -> None:
    current = int(time.time() * 1000)
    with service.db.session() as session:
        session.add(TelegramUser(telegram_user_id=42, last_seen_at_ms=current))
        session.flush()
        session.add(GroupMembership(
            telegram_user_id=42,
            group_chat_id=-100,
            status="member",
            legal=True,
            updated_at_ms=current - 10_000,
        ))

    assert service.user_is_eligible_cached(42)
    assert not service.user_is_eligible_cached(42, max_age_ms=5_000)


def test_pricing_snapshot_exposes_effective_rules_without_internal_auth_patterns(service) -> None:
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 12345)
    snapshot = service.pricing_snapshot("cycle")
    assert snapshot["active_version"]["name"] == "cpamp-initial"
    assert snapshot["models"][0]["default"]["input"]["usd_per_million"] == "1"
    assert snapshot["billing"]["pools"][0]["fixed_cost_cents"] == 12345
    assert "auth_index_pattern" not in json.dumps(snapshot)


def test_realtime_status_removes_key_ids_auth_indexes_and_request_particles(service) -> None:
    sanitized = service._sanitize_realtime({
        "current_usage": {
            "models": [{"key": "internal-model-id", "label": "gpt-test", "requests": 1}],
            "api_keys": [{"key": "42", "label": "sk-secret", "requests": 1, "tokens": 2, "cost": 0.1}],
            "auth_files": [{"key": "raw-auth-index", "label": "account", "requests": 1}],
            "ai_providers": [{"key": "provider-secret", "label": "provider", "requests": 1}],
        },
        "response_distribution": {
            "ttft": {"average_line": [], "particles": [{"timestamp": "secret"}], "total_particles": 1},
            "latency": {"average_line": [], "particles": [{"timestamp": "secret"}], "total_particles": 1},
        },
    })
    serialized = json.dumps(sanitized)
    assert "sk-secret" not in serialized
    assert "raw-auth-index" not in serialized
    assert "provider-secret" not in serialized
    assert "internal-model-id" not in serialized
    assert '"particles"' not in serialized
    assert '"timestamp": "secret"' not in serialized


def test_request_history_exposes_and_filters_true_tps(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        output_tokens=100,
        latency_ms=2100,
        ttft_ms=100,
    )
    service.sync_cpamp()
    service.rate_events()

    history = service.request_history(2, min_tps=49, max_tps=51, sort="tps_desc")
    assert history["pagination"]["total"] == 1
    assert history["items"][0]["generation_ms"] == 2000
    assert history["items"][0]["tps"] == 50.0
    assert history["summary"]["input_tokens"] == 1000
    assert history["summary"]["output_tokens"] == 100


def test_request_history_exposes_one_effective_cache_read_value(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(
        settings,
        cpamp_key_hash("key"),
        1000,
        cached_tokens=65024,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    insert_event(
        settings,
        cpamp_key_hash("key"),
        2000,
        event_hash="fine-grained-cache",
        cached_tokens=150,
        cache_read_tokens=100,
        cache_creation_tokens=50,
    )
    service.sync_cpamp()
    service.rate_events()

    items = service.request_history(2, sort="time_asc")["items"]
    assert items[0]["tokens"]["cache_read"] == 65024
    assert items[1]["tokens"]["cache_read"] == 100
    assert items[1]["tokens"]["cache_creation"] == 50
    assert all("cached" not in item["tokens"] for item in items)


def test_request_history_exposes_reasoning_effort_and_backfills_existing_events(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000, reasoning_effort="high")
    service.sync_cpamp()
    with service.db.session() as session:
        event = session.scalar(select(RawUsageEvent))
        event.reasoning_effort = None
    service.sync_cpamp()

    item = service.request_history(2)["items"][0]
    assert item["reasoning_effort"] == "high"


def test_gradient_updates_only_open_cycles_and_cannot_be_deleted_while_bound(service) -> None:
    rule_id = service.create_gradient_rule(
        "team-gradient",
        "test",
        [{"left": 0, "right": None, "multiplier": 1}],
        "create test rule",
    )
    service.create_cycle("open-cycle", "1970-01-01T08:00", "1970-01-02T08:00", 0, gradient_rule_id=rule_id)
    service.create_cycle("closed-cycle", "1970-01-02T08:00", "1970-01-03T08:00", 0, gradient_rule_id=rule_id)
    service.close_cycle("closed-cycle", 1, False)

    service.update_gradient_rule(
        rule_id,
        "team-gradient",
        "updated",
        [
            {"left": 0, "right": 10, "multiplier": 1},
            {"left": 10, "right": None, "multiplier": 0.5},
        ],
        "update test rule",
    )
    with service.db.session() as session:
        opened = session.scalar(select(BillingCycle).where(BillingCycle.name == "open-cycle"))
        closed = session.scalar(select(BillingCycle).where(BillingCycle.name == "closed-cycle"))
        assert json.loads(opened.tiers_json)[0]["right"] == "10"
        assert json.loads(closed.tiers_json)[0]["right"] is None
    with pytest.raises(BillingError, match="仍被未关闭账期使用"):
        service.delete_gradient_rule(rule_id, "cannot delete yet")


def test_unowned_metered_key_reduces_member_pool_cost(service, settings) -> None:
    create_owner(service, "owned-key", 2, 0)
    with service.db.session() as session:
        session.add(APIKey(
            cpamp_hash=cpamp_key_hash("metered-key"),
            login_fingerprint=None,
            masked_value="sk-cpa-****metered",
            status="unowned",
            current_owner_id=None,
            present_in_cpa=True,
            created_at_ms=0,
        ))
        session.flush()
        metered_id = session.scalar(select(APIKey.id).where(APIKey.cpamp_hash == cpamp_key_hash("metered-key")))
    service.update_unowned_key_profile(metered_id, "external-team", "7", "set RMB per USD multiplier")
    insert_event(
        settings,
        cpamp_key_hash("owned-key"),
        1000,
        event_hash="owned-usage",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=0,
    )
    insert_event(
        settings,
        cpamp_key_hash("metered-key"),
        1001,
        event_hash="metered-usage",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=0,
    )
    service.sync_cpamp()
    service.rate_events()
    service.create_cycle("metered-cycle", "1970-01-01T08:00", "1970-01-02T08:00", 1000)

    dashboard = service.dashboard("metered-cycle")
    owned = next(row for row in dashboard["rows"] if row["telegram_user_id"] == 2)
    unowned = next(row for row in dashboard["rows"] if row["unowned"])
    assert owned["amount"] == "3.00"
    assert unowned["amount"] == "7.00"
    assert dashboard["totals"]["fixed_cost"] == "10.00"
    assert dashboard["totals"]["metered_amount"] == "7.00"
    assert dashboard["totals"]["member_amount"] == "3.00"
    assert dashboard["totals"]["amount"] == "10.00"


def test_cpa_key_sync_restores_owned_key_status(service, monkeypatch) -> None:
    create_owner(service, "owned-key", 2, 0)
    cpa_keys: list[str] = []
    monkeypatch.setattr(service.cpa, "list_keys", lambda: list(cpa_keys))

    service.sync_cpa_keys()
    with service.db.session() as session:
        key = session.scalar(select(APIKey).where(APIKey.cpamp_hash == cpamp_key_hash("owned-key")))
        assert key.status == "retired"
        assert key.present_in_cpa is False

    cpa_keys.append("owned-key")
    service.sync_cpa_keys()
    with service.db.session() as session:
        key = session.scalar(select(APIKey).where(APIKey.cpamp_hash == cpamp_key_hash("owned-key")))
        assert key.status == "active"
        assert key.present_in_cpa is True
        assert key.login_fingerprint is not None


def test_upstream_price_sync_rerates_open_cycles_only(service, settings, monkeypatch) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp()
    service.rate_events()
    service.create_cycle("open-price", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    service.create_cycle("closed-price", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    service.close_cycle("closed-price", 1, False)
    before_open = service.dashboard("open-price")["totals"]["actual"]
    before_closed = service.dashboard("closed-price")["totals"]["actual"]

    def sync_prices(models):
        assert "gpt-test" in models
        db = sqlite3.connect(settings.cpamp_database_path)
        db.execute(
            "update model_prices set prompt_per_1m=2, completion_per_1m=12, cache_per_1m=.2, "
            "cache_read_per_1m=.2, cache_creation_per_1m=2.5 where model='gpt-test'"
        )
        db.commit()
        db.close()
        return {"source": "test", "sources": ["test"], "imported": 1, "skipped": 0, "unmatched": []}

    monkeypatch.setattr(service.cpamp, "sync_model_prices", sync_prices)
    result = service.sync_upstream_prices("synced-prices", "web-admin", "admin-token", "test refresh")
    assert result["rated_events"] == 0
    assert result["rating_status"] == "queued"
    assert service.rate_events(limit=500) >= 1
    assert Decimal(service.dashboard("open-price")["totals"]["actual"].replace(",", "")) > Decimal(before_open.replace(",", ""))
    assert service.dashboard("closed-price")["totals"]["actual"] == before_closed
    assert service.request_history(2)["items"][0]["cost"] == before_closed
    with service.db.session() as session:
        opened = session.scalar(select(BillingCycle).where(BillingCycle.name == "open-price"))
        closed = session.scalar(select(BillingCycle).where(BillingCycle.name == "closed-price"))
        assert session.get(PricingVersion, opened.pricing_version_id).name == "synced-prices"
        assert session.get(PricingVersion, closed.pricing_version_id).name == "cpamp-initial"


def test_manual_price_update_creates_version_and_rerates_open_cycles_only(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp()
    service.rate_events()
    service.create_cycle("open-manual-price", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    service.create_cycle("closed-manual-price", "1970-01-01T08:00", "1970-01-02T08:00", 1000)
    service.close_cycle("closed-manual-price", 1, False)
    before_open = service.dashboard("open-manual-price")["totals"]["actual"]
    before_closed = service.dashboard("closed-manual-price")["totals"]["actual"]

    result = service.update_pricing_rule(
        "gpt-test",
        {
            "input_nano_per_token": 2_000,
            "output_nano_per_token": 12_000,
            "cache_read_nano_per_token": 200,
            "cache_creation_nano_per_token": 2_500,
            "priority_input_nano_per_token": None,
            "priority_output_nano_per_token": None,
            "priority_cache_read_nano_per_token": None,
            "priority_cache_creation_nano_per_token": None,
            "flex_input_nano_per_token": None,
            "flex_output_nano_per_token": None,
            "long_threshold_tokens": None,
            "long_input_multiplier_ppm": 1_000_000,
            "long_output_multiplier_ppm": 1_000_000,
        },
        "manual-prices",
        "修正测试价格",
        "web-admin",
        "admin-token",
    )

    assert result["name"] == "manual-prices"
    assert result["rated_events"] == 0
    assert result["rating_status"] == "queued"
    assert service.rate_events(limit=500) >= 1
    assert Decimal(service.dashboard("open-manual-price")["totals"]["actual"].replace(",", "")) > Decimal(before_open.replace(",", ""))
    assert service.dashboard("closed-manual-price")["totals"]["actual"] == before_closed
    with service.db.session() as session:
        opened = session.scalar(select(BillingCycle).where(BillingCycle.name == "open-manual-price"))
        closed = session.scalar(select(BillingCycle).where(BillingCycle.name == "closed-manual-price"))
        assert session.get(PricingVersion, opened.pricing_version_id).name == "manual-prices"
        assert session.get(PricingVersion, closed.pricing_version_id).name == "cpamp-initial"


def test_read_pages_do_not_rate_events(service, settings, monkeypatch) -> None:
    current = int(time.time() * 1000)
    zone = ZoneInfo(settings.timezone)
    start = (datetime.fromtimestamp(current / 1000, zone) - timedelta(hours=1)).isoformat()
    end = (datetime.fromtimestamp(current / 1000, zone) + timedelta(hours=1)).isoformat()
    service.create_cycle("pending-pages", start, end, 0)
    insert_event(settings, "pending-key", current - 1000, event_hash="pending-page-event")
    service.sync_cpamp()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("read pages must not synchronously rate events")

    monkeypatch.setattr(service, "_rate_until_current", fail_if_called, raising=False)

    dashboard = service.dashboard("pending-pages")
    assert dashboard["cycle"]["unpriced_events"] == 1

    monkeypatch.setattr(service.cpa, "auth_files", lambda: [])
    assert service.site_status("24h")["overview"]["summary"]["unpriced_events"] == 1
    assert service.accounts_snapshot()["accounts"] == []


def test_site_status_usage_is_calculated_locally(service, settings, monkeypatch) -> None:
    current = int(time.time() * 1000)
    insert_event(settings, "site-key", current - 1000, event_hash="site-local")
    service.sync_cpamp()
    service.rate_events()

    monkeypatch.setattr(service.cpa, "auth_files", lambda: [])
    status = service.site_status("24h")
    assert status["overview"]["summary"]["request_count"] == 1
    assert status["overview"]["summary"]["token_count"] == 1100
    assert status["overview"]["summary"]["source"] == "billing-panel"
    assert status["realtime"]["source"] == "billing-panel"


def test_site_status_supports_shared_ranges_and_cache_hit_rate(service, settings, monkeypatch) -> None:
    current = int(time.time() * 1000)
    insert_event(
        settings,
        "cache-key",
        current - 1000,
        event_hash="cache-hit",
        input_tokens=1000,
        cached_tokens=0,
        cache_read_tokens=500,
    )
    service.sync_cpamp()
    service.rate_events()
    monkeypatch.setattr(service.cpa, "auth_files", lambda: [])

    recent = service.site_status("60m")
    assert recent["range"]["name"] == "60m"
    assert recent["realtime"]["range"] == "60m"
    assert any(item["cache_hit_rate"] == 50.0 for item in recent["realtime"]["cache_level"])

    all_time = service.site_status("all")
    assert all_time["range"]["name"] == "all"
    assert all_time["range"]["start"] is not None
    assert all_time["overview"]["summary"]["request_count"] == 1


def test_rankings_keep_unowned_keys_separate_and_masked(service, settings) -> None:
    current = int(time.time() * 1000)
    with service.db.session() as session:
        session.add_all([
            APIKey(
                cpamp_hash=cpamp_key_hash("unowned-one"),
                login_fingerprint=None,
                masked_value="sk-cpa-****one",
                display_name="external-one",
                status="unowned",
                current_owner_id=None,
                created_at_ms=current,
            ),
            APIKey(
                cpamp_hash=cpamp_key_hash("unowned-two"),
                login_fingerprint=None,
                masked_value="sk-cpa-****two",
                status="unowned",
                current_owner_id=None,
                created_at_ms=current,
            ),
        ])
    insert_event(settings, cpamp_key_hash("unowned-one"), current - 1000, event_hash="unowned-one-event")
    insert_event(settings, cpamp_key_hash("unowned-two"), current - 500, event_hash="unowned-two-event")
    service.sync_cpamp()
    service.rate_events()

    web_rows = [row for row in service.ranking_snapshot("24h")["rows"] if row["unowned"]]
    assert {row["name"] for row in web_rows} == {"external-one", "sk-cpa-****two"}
    assert all(row["key_count"] == 1 for row in web_rows)
    bot_rows = [row for row in service.rankings(current - 86_400_000) if row["telegram_user_id"] is None]
    assert {row["name"] for row in bot_rows} == {"external-one", "sk-cpa-****two"}
    _, chart = service.hourly_usage(24)
    assert {row["name"] for row in chart} == {"external-one", "sk-cpa-****two"}


def test_reconciliation_does_not_warn_for_fresh_transient_sync_lag(service, settings) -> None:
    insert_event(settings, "first", 1000, event_hash="first")
    service.sync_cpamp()
    service.rate_events()
    insert_event(settings, "second", 2000, event_hash="second")

    result = service.reconciliation()
    assert result["sync_backlog"] == 1
    assert result["sync_pending"] is True
    assert result["sync_stale"] is False
    assert result["sync_degraded"] is False
    assert result["ok"] is True

    with service.db.session() as session:
        checkpoint = session.scalar(select(SyncCheckpoint))
        checkpoint.last_success_at_ms = 1
    assert service.reconciliation()["sync_degraded"] is True
