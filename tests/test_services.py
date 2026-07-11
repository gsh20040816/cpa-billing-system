from __future__ import annotations

import hashlib
import json
import sqlite3

from sqlalchemy import func, select

from cpa_billing.models import APIKey, BillingCycle, CyclePoolCost, KeyOwnershipPeriod, RatedEvent, RawUsageEvent, ResourcePool, Statement, TelegramUser
from cpa_billing.security import cpamp_key_hash


def insert_event(settings, key_hash: str, timestamp_ms: int, *, event_hash: str = "e1", input_tokens: int = 1000,
                 cached_tokens: int = 100, output_tokens: int = 100, tier: str = "default") -> None:
    db = sqlite3.connect(settings.cpamp_database_path)
    db.execute("""insert into usage_events(event_hash,request_id,timestamp_ms,timestamp,provider,executor_type,model,
               requested_model,resolved_model,service_tier,api_key_hash,source_hash,source,account_snapshot,auth_index,
               input_tokens,output_tokens,reasoning_tokens,cached_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,
               failed,fail_status_code,latency_ms,ttft_ms) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (event_hash, "r1", timestamp_ms, "2026-07-04T00:00:00Z", "codex", "CodexExecutor", "gpt-test", "gpt-test", "gpt-test", tier,
                key_hash, "source", "masked", "account", "auth", input_tokens, output_tokens, 40, cached_tokens, 0, 0,
                input_tokens + output_tokens, 0, None, 100, 10))
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


def test_rating_uses_cached_subset_and_reasoning_not_added(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); assert service.rate_events() == 1
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        assert rated.telegram_user_id == 2
        assert rated.rated_weight_nano_usd == 900 * 1000 + 100 * 100 + 100 * 6000


def test_priority_and_long_context_combine(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000, input_tokens=300000, cached_tokens=0, output_tokens=10, tier="fast")
    service.sync_cpamp(); service.rate_events()
    with service.db.session() as session:
        rated = session.scalar(select(RatedEvent))
        detail = json.loads(rated.calculation_json)
        assert rated.long_context_applied is True
        assert rated.service_tier == "priority"
        assert detail["rates"][0] == 4000


def test_cycle_allocation_groups_by_user_and_preserves_cost(service, settings) -> None:
    create_owner(service, "key1", 2, 0); create_owner(service, "key2", 3, 0)
    insert_event(settings, cpamp_key_hash("key1"), 1000, event_hash="a", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    insert_event(settings, cpamp_key_hash("key2"), 1001, event_hash="b", input_tokens=2_000_000, cached_tokens=0, output_tokens=0)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 109000)
    statements = service.preview_cycle("cycle")
    assert sum(row.amount_cents for row in statements) == 109000
    assert {row.telegram_user_id for row in statements} == {2, 3}


def test_unowned_events_do_not_create_statement(service, settings) -> None:
    insert_event(settings, "unowned", 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    assert service.preview_cycle("cycle") == []


def test_closed_cycle_is_immutable(service, settings) -> None:
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    service.close_cycle("cycle", 1, False)
    first = service.preview_cycle("cycle")
    assert first[0].final is True
