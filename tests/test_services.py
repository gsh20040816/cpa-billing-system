from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from cpa_billing.database import Database
from cpa_billing.models import APIKey, Adjustment, BillingCycle, CyclePoolCost, KeyOwnershipPeriod, RatedEvent, RawUsageEvent, ResourcePool, Statement, TelegramUser
from cpa_billing.security import cpamp_key_hash
from cpa_billing.services import BillingError, BillingService


def insert_event(settings, key_hash: str, timestamp_ms: int, *, event_hash: str = "e1", input_tokens: int = 1000,
                 cached_tokens: int = 100, output_tokens: int = 100, tier: str = "default",
                 model: str = "gpt-test", failed: bool = False, fail_status_code: int | None = None,
                 latency_ms: int = 100, ttft_ms: int = 10) -> None:
    db = sqlite3.connect(settings.cpamp_database_path)
    db.execute("""insert into usage_events(event_hash,request_id,timestamp_ms,timestamp,provider,executor_type,model,
               requested_model,resolved_model,service_tier,api_key_hash,source_hash,source,account_snapshot,auth_index,
               input_tokens,output_tokens,reasoning_tokens,cached_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,
               failed,fail_status_code,latency_ms,ttft_ms) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (event_hash, f"request-{event_hash}", timestamp_ms, "2026-07-04T00:00:00Z", "codex", "CodexExecutor", model, model, model, tier,
                key_hash, "source", "masked", "account", "auth", input_tokens, output_tokens, 40, cached_tokens, 0, 0,
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


def test_keeper_accounts_are_sanitized_and_refresh_uses_public_account_ids(service, monkeypatch) -> None:
    raw = {
        "identities": [{
            "id": "7",
            "name": "secret@example.com",
            "displayName": "Shared Pro",
            "identity": "raw-auth-index",
            "file_name": "secret.json",
            "file_path": "/root/private/secret.json",
            "type": "codex",
            "provider": "codex",
            "auth_type_name": "oauth",
            "plan_type": "pro",
            "disabled": False,
            "total_requests": 10,
            "success_count": 9,
            "failure_count": 1,
            "total_tokens": 1234,
        }],
        "quota": {"items": [{
            "auth_index": "raw-auth-index",
            "file_name": "secret.json",
            "status": "completed",
            "refreshed_at": "2026-07-11T12:00:00+08:00",
            "quota": {"quota": [{
                "key": "rate_limit.primary_window",
                "label": "5h",
                "usedPercent": 42,
                "allowed": True,
                "limitReached": False,
                "window": {"seconds": 18000},
            }]},
        }]},
        "inspection": {"total": 1, "normal": 1, "results": [{
            "auth_index": "raw-auth-index", "file_name": "secret.json", "name": "Shared Pro", "status": "normal",
        }]},
    }

    class FakeKeeper:
        def accounts_snapshot(self):
            return raw

        def refresh(self, auth_indexes):
            assert auth_indexes == ["raw-auth-index"]
            return {"tasks": [{"authIndex": "raw-auth-index"}], "accepted": 1, "skipped": 0, "limit": 1}

    monkeypatch.setattr(service, "keeper", FakeKeeper())
    snapshot = service.accounts_snapshot()
    serialized = json.dumps(snapshot)
    assert "raw-auth-index" not in serialized
    assert "secret.json" not in serialized
    assert "/root/private" not in serialized
    assert snapshot["accounts"][0]["id"] == "7"
    assert snapshot["accounts"][0]["quota"][0]["used_percent"] == 42

    refresh = service.refresh_account_quotas(["7"])
    assert refresh["tasks"] == [{"account_id": "7", "status": "queued"}]


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
