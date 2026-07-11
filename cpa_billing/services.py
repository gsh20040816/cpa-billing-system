from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .database import Database, now_ms
from .domain import NANO_USD, format_cents, format_usd_nano, largest_remainder, parse_tiers, tiered_weight
from .models import (
    APIKey,
    AllowedChat,
    Adjustment,
    AuditLog,
    BillingCycle,
    CPAMPSource,
    CyclePoolCost,
    DeadLetter,
    GroupMembership,
    KeyActionRequest,
    KeyOwnershipPeriod,
    ModelPriceRule,
    PoolAssignmentRule,
    PricingVersion,
    RatedEvent,
    RawUsageEvent,
    ReconciliationRun,
    ResourcePool,
    Statement,
    StatementLine,
    SyncCheckpoint,
    TelegramUser,
    WebSession,
)
from .security import (
    constant_equal,
    cpamp_key_hash,
    generate_api_key,
    hash_token,
    login_fingerprint,
    mask_api_key,
    mask_hash,
    secure_token,
)


DEFAULT_TIERS = [
    {"left": 0, "right": 300, "multiplier": 1},
    {"left": 300, "right": 800, "multiplier": 0.9},
    {"left": 800, "right": 1400, "multiplier": 0.8},
    {"left": 1400, "right": 2000, "multiplier": 0.7},
    {"left": 2000, "right": None, "multiplier": 0.6},
]


class BillingError(RuntimeError):
    pass


class CPAClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.cpa_base_url
        self.key = settings.cpa_management_key

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.key}"
        with httpx.Client(timeout=15) as client:
            response = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def list_keys(self) -> list[str]:
        data = self._request("GET", "/v0/management/api-keys")
        keys = data.get("api-keys") if isinstance(data, dict) else None
        if not isinstance(keys, list):
            raise BillingError("CPA api-keys response is invalid")
        return [str(key) for key in keys]

    def key_is_active(self, raw_key: str) -> bool:
        candidate = cpamp_key_hash(raw_key)
        return any(constant_equal(cpamp_key_hash(key), candidate) for key in self.list_keys())

    def add_key(self, raw_key: str) -> None:
        keys = self.list_keys()
        if any(constant_equal(key, raw_key) for key in keys):
            return
        self._request("PUT", "/v0/management/api-keys", json=keys + [raw_key])

    def remove_key_hash(self, key_hash: str) -> None:
        keys = self.list_keys()
        remaining = [key for key in keys if not constant_equal(cpamp_key_hash(key), key_hash)]
        if len(remaining) != len(keys):
            self._request("PUT", "/v0/management/api-keys", json=remaining)


def _nano_per_token(value: Any) -> int:
    return int((Decimal(str(value or 0)) * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))


def _ppm(value: Decimal) -> int:
    return int((value * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


class BillingService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database
        self.cpa = CPAClient(settings)

    def bootstrap(self) -> None:
        self.db.initialize()
        now = now_ms()
        with self.db.session() as session:
            source = session.scalar(select(CPAMPSource).where(CPAMPSource.name == self.settings.cpamp_source_name))
            if source is None:
                source = CPAMPSource(name=self.settings.cpamp_source_name, created_at_ms=now, schema_fingerprint="")
                session.add(source)
                session.flush()
                session.add(SyncCheckpoint(source_id=source.id, last_event_id=0, backlog=0))
            pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
            if pool is None:
                pool = ResourcePool(name="default-cpa", active=True, created_at_ms=now)
                session.add(pool)
                session.flush()
                session.add(PoolAssignmentRule(pool_id=pool.id, priority=1000, active=True))
        self.import_cpamp_prices("cpamp-initial")

    def _cpamp(self) -> sqlite3.Connection:
        uri = f"file:{self.settings.cpamp_database_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma query_only=on")
        connection.execute("pragma busy_timeout=15000")
        return connection

    def _schema_fingerprint(self, connection: sqlite3.Connection) -> str:
        row = connection.execute("select sql from sqlite_master where type='table' and name='usage_events'").fetchone()
        if row is None:
            raise BillingError("CPAMP usage_events table is missing")
        required = {"id", "event_hash", "timestamp_ms", "api_key_hash", "model", "input_tokens", "output_tokens"}
        columns = {str(item[1]) for item in connection.execute("pragma table_info(usage_events)")}
        missing = required - columns
        if missing:
            raise BillingError("CPAMP schema missing columns: " + ", ".join(sorted(missing)))
        return hashlib.sha256(str(row[0]).encode()).hexdigest()

    def sync_cpamp(self, batch_size: int = 1000) -> int:
        imported = 0
        with self._cpamp() as source_db:
            fingerprint = self._schema_fingerprint(source_db)
            with self.db.session() as session:
                source = session.scalar(select(CPAMPSource).where(CPAMPSource.name == self.settings.cpamp_source_name))
                if source is None:
                    raise BillingError("bootstrap has not created CPAMP source")
                checkpoint = session.get(SyncCheckpoint, source.id)
                if checkpoint is None:
                    raise BillingError("sync checkpoint is missing")
                if source.schema_fingerprint and source.schema_fingerprint != fingerprint:
                    checkpoint.last_error = "CPAMP schema fingerprint changed; review required"
                    raise BillingError(checkpoint.last_error)
                source.schema_fingerprint = fingerprint
                rows = source_db.execute(
                    """
                    select id,event_hash,request_id,timestamp_ms,timestamp,provider,executor_type,model,
                           requested_model,resolved_model,service_tier,api_key_hash,source_hash,source,
                           account_snapshot,auth_index,input_tokens,output_tokens,reasoning_tokens,
                           cached_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,failed,
                           fail_status_code,latency_ms,ttft_ms,response_metadata_json,header_quota_used_percent,
                           header_quota_recover_at_ms,header_quota_plan_type
                    from usage_events where id > ? order by id limit ?
                    """,
                    (checkpoint.last_event_id, batch_size),
                ).fetchall()
                for row in rows:
                    try:
                        event = RawUsageEvent(
                            source_id=source.id,
                            source_event_id=int(row["id"]),
                            event_hash=str(row["event_hash"]),
                            request_id=row["request_id"],
                            occurred_at_ms=int(row["timestamp_ms"]),
                            timestamp=str(row["timestamp"]),
                            provider=row["provider"], executor_type=row["executor_type"], model=str(row["model"]),
                            requested_model=row["requested_model"], resolved_model=row["resolved_model"],
                            service_tier=row["service_tier"], api_key_hash=row["api_key_hash"], source_hash=row["source_hash"],
                            source_label=row["source"], account_snapshot=row["account_snapshot"], auth_index=row["auth_index"],
                            input_tokens=int(row["input_tokens"] or 0), output_tokens=int(row["output_tokens"] or 0),
                            reasoning_tokens=int(row["reasoning_tokens"] or 0), cached_tokens=int(row["cached_tokens"] or 0),
                            cache_read_tokens=int(row["cache_read_tokens"] or 0),
                            cache_creation_tokens=int(row["cache_creation_tokens"] or 0), total_tokens=int(row["total_tokens"] or 0),
                            failed=bool(row["failed"]), fail_status_code=row["fail_status_code"],
                            latency_ms=row["latency_ms"], ttft_ms=row["ttft_ms"], response_metadata_json=row["response_metadata_json"],
                            quota_used_percent=None if row["header_quota_used_percent"] is None else int(Decimal(str(row["header_quota_used_percent"])) * 1_000_000),
                            quota_recover_at_ms=row["header_quota_recover_at_ms"], quota_plan_type=row["header_quota_plan_type"], imported_at_ms=now_ms(),
                        )
                        with session.begin_nested():
                            session.add(event)
                            session.flush()
                        imported += 1
                    except IntegrityError:
                        pass
                    except Exception as exc:
                        session.add(DeadLetter(source_id=source.id, source_event_id=int(row["id"]), error=str(exc), payload_json="{}", created_at_ms=now_ms()))
                    checkpoint.last_event_id = int(row["id"])
                    checkpoint.last_event_at_ms = int(row["timestamp_ms"])
                maximum = int(source_db.execute("select coalesce(max(id),0) from usage_events").fetchone()[0])
                checkpoint.backlog = max(0, maximum - checkpoint.last_event_id)
                checkpoint.last_success_at_ms = now_ms()
                checkpoint.last_error = None
        return imported

    def import_cpamp_prices(self, name: str) -> int:
        with self.db.session() as session:
            existing = session.scalar(select(PricingVersion).where(PricingVersion.name == name))
            if existing is not None:
                return existing.id
        with self._cpamp() as source_db:
            rows = source_db.execute("select * from model_prices order by model").fetchall()
        with self.db.session() as session:
            for active in session.scalars(select(PricingVersion).where(PricingVersion.status == "active")):
                active.status = "retired"
            version = PricingVersion(name=name, status="active", source="CPAMP model_prices snapshot", created_at_ms=now_ms(), activated_at_ms=now_ms())
            session.add(version)
            session.flush()
            for row in rows:
                raw_text = str(row["raw_json"] or "")
                try:
                    raw = json.loads(raw_text) if raw_text else {}
                except json.JSONDecodeError:
                    raw = {}
                base_input = Decimal(str(row["prompt_per_1m"] or 0))
                base_output = Decimal(str(row["completion_per_1m"] or 0))
                above_input = Decimal(str(raw.get("input_cost_per_token_above_272k_tokens", 0))) * Decimal(1_000_000)
                above_output = Decimal(str(raw.get("output_cost_per_token_above_272k_tokens", 0))) * Decimal(1_000_000)
                session.add(ModelPriceRule(
                    pricing_version_id=version.id, model=str(row["model"]),
                    input_nano_per_token=_nano_per_token(row["prompt_per_1m"]),
                    output_nano_per_token=_nano_per_token(row["completion_per_1m"]),
                    cache_read_nano_per_token=_nano_per_token(row["cache_read_per_1m"] or row["cache_per_1m"]),
                    cache_creation_nano_per_token=_nano_per_token(row["cache_creation_per_1m"]),
                    priority_input_nano_per_token=_nano_per_token(Decimal(str(raw.get("input_cost_per_token_priority", 0))) * Decimal(1_000_000)) or None,
                    priority_output_nano_per_token=_nano_per_token(Decimal(str(raw.get("output_cost_per_token_priority", 0))) * Decimal(1_000_000)) or None,
                    priority_cache_read_nano_per_token=_nano_per_token(Decimal(str(raw.get("cache_read_input_token_cost_priority", 0))) * Decimal(1_000_000)) or None,
                    priority_cache_creation_nano_per_token=_nano_per_token(Decimal(str(raw.get("cache_creation_input_token_cost_priority", 0))) * Decimal(1_000_000)) or None,
                    flex_input_nano_per_token=_nano_per_token(Decimal(str(raw.get("input_cost_per_token_flex", 0))) * Decimal(1_000_000)) or None,
                    flex_output_nano_per_token=_nano_per_token(Decimal(str(raw.get("output_cost_per_token_flex", 0))) * Decimal(1_000_000)) or None,
                    long_threshold_tokens=272000 if above_input or above_output else None,
                    long_input_multiplier_ppm=_ppm(above_input / base_input) if above_input and base_input else 1_000_000,
                    long_output_multiplier_ppm=_ppm(above_output / base_output) if above_output and base_output else 1_000_000,
                    raw_json=raw_text or None,
                ))
            return version.id

    def _active_pricing_id(self, session: Any) -> int:
        version = session.scalar(select(PricingVersion).where(PricingVersion.status == "active").order_by(PricingVersion.id.desc()))
        if version is None:
            raise BillingError("no active pricing version")
        return version.id

    def _owner_at(self, session: Any, key_hash: str | None, occurred_at_ms: int) -> int | None:
        if not key_hash:
            return None
        key = session.scalar(select(APIKey).where(APIKey.cpamp_hash == key_hash))
        if key is None:
            return None
        period = session.scalar(
            select(KeyOwnershipPeriod).where(
                KeyOwnershipPeriod.api_key_id == key.id,
                KeyOwnershipPeriod.valid_from_ms <= occurred_at_ms,
                or_(KeyOwnershipPeriod.valid_to_ms.is_(None), KeyOwnershipPeriod.valid_to_ms > occurred_at_ms),
            ).order_by(KeyOwnershipPeriod.valid_from_ms.desc())
        )
        return period.telegram_user_id if period else None

    def _pool_for(self, session: Any, event: RawUsageEvent) -> int | None:
        rules = session.scalars(select(PoolAssignmentRule).where(PoolAssignmentRule.active.is_(True)).order_by(PoolAssignmentRule.priority)).all()
        for rule in rules:
            if rule.auth_index_pattern and not re.search(rule.auth_index_pattern, event.auth_index or ""):
                continue
            if rule.model_pattern and not re.search(rule.model_pattern, event.resolved_model or event.model):
                continue
            return rule.pool_id
        return None

    def rate_events(self, limit: int = 2000) -> int:
        rated = 0
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            events = session.scalars(
                select(RawUsageEvent).outerjoin(
                    RatedEvent,
                    and_(RatedEvent.raw_event_id == RawUsageEvent.id, RatedEvent.pricing_version_id == version_id),
                ).where(RatedEvent.id.is_(None)).order_by(RawUsageEvent.id).limit(limit)
            ).all()
            prices = {rule.model: rule for rule in session.scalars(select(ModelPriceRule).where(ModelPriceRule.pricing_version_id == version_id))}
            for event in events:
                model = event.resolved_model or event.model
                rule = prices.get(model)
                if rule is None:
                    continue
                tier = (event.service_tier or "default").lower()
                if tier == "fast":
                    tier = "priority"
                input_rate = rule.input_nano_per_token
                output_rate = rule.output_nano_per_token
                cache_rate = rule.cache_read_nano_per_token
                creation_rate = rule.cache_creation_nano_per_token
                if tier == "priority":
                    input_rate = rule.priority_input_nano_per_token or input_rate
                    output_rate = rule.priority_output_nano_per_token or output_rate
                    cache_rate = rule.priority_cache_read_nano_per_token or cache_rate
                    creation_rate = rule.priority_cache_creation_nano_per_token or creation_rate
                elif tier == "flex":
                    input_rate = rule.flex_input_nano_per_token or input_rate
                    output_rate = rule.flex_output_nano_per_token or output_rate
                long_context = bool(rule.long_threshold_tokens and event.input_tokens > rule.long_threshold_tokens)
                if long_context:
                    input_rate = (input_rate * rule.long_input_multiplier_ppm + 500_000) // 1_000_000
                    cache_rate = (cache_rate * rule.long_input_multiplier_ppm + 500_000) // 1_000_000
                    creation_rate = (creation_rate * rule.long_input_multiplier_ppm + 500_000) // 1_000_000
                    output_rate = (output_rate * rule.long_output_multiplier_ppm + 500_000) // 1_000_000
                cached = max(event.cached_tokens, event.cache_read_tokens)
                uncached = max(0, event.input_tokens - cached - event.cache_creation_tokens)
                cost = uncached * input_rate + cached * cache_rate + event.cache_creation_tokens * creation_rate + event.output_tokens * output_rate
                detail = {"model": model, "tier": tier, "uncached": uncached, "cached": cached, "cache_creation": event.cache_creation_tokens,
                          "output": event.output_tokens, "rates": [input_rate, cache_rate, creation_rate, output_rate]}
                session.add(RatedEvent(
                    raw_event_id=event.id, pricing_version_id=version_id, pool_id=self._pool_for(session, event),
                    telegram_user_id=self._owner_at(session, event.api_key_hash, event.occurred_at_ms),
                    occurred_at_ms=event.occurred_at_ms, rated_weight_nano_usd=cost,
                    long_context_applied=long_context, service_tier=tier, calculation_json=json.dumps(detail, separators=(",", ":")), rated_at_ms=now_ms(),
                ))
                rated += 1
        return rated

    def upsert_user(self, user: dict[str, Any], registered: bool = False) -> TelegramUser:
        user_id = int(user["id"])
        with self.db.session() as session:
            row = session.get(TelegramUser, user_id)
            if row is None:
                row = TelegramUser(telegram_user_id=user_id, last_seen_at_ms=now_ms(), is_admin=user_id in self.settings.admin_user_ids)
                session.add(row)
            row.username = user.get("username") or None
            row.first_name = user.get("first_name") or None
            row.last_name = user.get("last_name") or None
            row.last_seen_at_ms = now_ms()
            if registered and row.registered_at_ms is None:
                row.registered_at_ms = now_ms()
            session.flush()
            return row

    def set_membership(self, user: dict[str, Any], group_id: int, status: str, legal: bool) -> None:
        self.upsert_user(user)
        with self.db.session() as session:
            row = session.get(GroupMembership, (int(user["id"]), group_id))
            if row is None:
                row = GroupMembership(telegram_user_id=int(user["id"]), group_chat_id=group_id, status=status, legal=legal, updated_at_ms=now_ms())
                session.add(row)
            else:
                row.status, row.legal, row.updated_at_ms = status, legal, now_ms()

    def user_is_eligible_cached(self, user_id: int) -> bool:
        if user_id in self.settings.admin_user_ids:
            return True
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user and user.manual_allowed:
                return True
            return bool(session.scalar(select(func.count()).select_from(GroupMembership).where(GroupMembership.telegram_user_id == user_id, GroupMembership.legal.is_(True))))

    def _insert_key(self, session: Any, raw_key: str, owner_id: int, source: str) -> APIKey:
        key_hash = cpamp_key_hash(raw_key)
        existing = session.scalar(select(APIKey).where(APIKey.cpamp_hash == key_hash))
        if existing is not None:
            raise BillingError("API Key already exists")
        row = APIKey(cpamp_hash=key_hash, login_fingerprint=login_fingerprint(raw_key, self.settings.key_pepper), masked_value=mask_api_key(raw_key),
                     status="active", current_owner_id=owner_id, created_at_ms=now_ms())
        session.add(row)
        session.flush()
        session.add(KeyOwnershipPeriod(api_key_id=row.id, telegram_user_id=owner_id, valid_from_ms=now_ms(), valid_to_ms=None,
                                       source=source, created_at_ms=now_ms()))
        return row

    def register_key(self, user: dict[str, Any]) -> str:
        user_id = int(user["id"])
        self.upsert_user(user, registered=True)
        raw_key = generate_api_key(self.settings.api_key_prefix)
        self.cpa.add_key(raw_key)
        try:
            with self.db.session() as session:
                self._insert_key(session, raw_key, user_id, "telegram-register")
                session.add(AuditLog(operator_type="telegram", operator_id=str(user_id), operation="key.register", target=mask_api_key(raw_key), created_at_ms=now_ms()))
        except Exception:
            self.cpa.remove_key_hash(cpamp_key_hash(raw_key))
            raise
        return raw_key

    def active_keys(self, user_id: int) -> list[APIKey]:
        with self.db.session() as session:
            return list(session.scalars(select(APIKey).where(APIKey.current_owner_id == user_id, APIKey.status == "active").order_by(APIKey.id)))

    def authenticate_key(self, raw_key: str) -> tuple[TelegramUser, APIKey] | None:
        fingerprint = login_fingerprint(raw_key, self.settings.key_pepper)
        with self.db.session() as session:
            key = session.scalar(select(APIKey).where(APIKey.login_fingerprint == fingerprint, APIKey.status == "active"))
            if key is None or key.current_owner_id is None:
                return None
            user = session.get(TelegramUser, key.current_owner_id)
            if user is None or user.registered_at_ms is None:
                return None
            key_id, user_id = key.id, user.telegram_user_id
        if not self.cpa.key_is_active(raw_key):
            return None
        with self.db.session() as session:
            return session.get(TelegramUser, user_id), session.get(APIKey, key_id)

    def create_session(self, user_id: int, api_key_id: int) -> tuple[str, str]:
        token, csrf = secure_token(), secure_token(18)
        with self.db.session() as session:
            session.add(WebSession(session_hash=hash_token(token, self.settings.session_secret), telegram_user_id=user_id, api_key_id=api_key_id,
                                   csrf_token=csrf, created_at_ms=now_ms(), expires_at_ms=now_ms() + self.settings.session_ttl_seconds * 1000))
        return token, csrf

    def get_session(self, token: str | None) -> tuple[WebSession, TelegramUser] | None:
        if not token:
            return None
        with self.db.session() as session:
            row = session.get(WebSession, hash_token(token, self.settings.session_secret))
            if row is None or row.revoked_at_ms is not None or row.expires_at_ms <= now_ms():
                return None
            user = session.get(TelegramUser, row.telegram_user_id)
            return (row, user) if user else None

    def revoke_session(self, token: str) -> None:
        with self.db.session() as session:
            row = session.get(WebSession, hash_token(token, self.settings.session_secret))
            if row:
                row.revoked_at_ms = now_ms()

    def request_key_action(self, user_id: int, raw_current_key: str, action: str, target_key_id: int | None) -> str:
        authenticated = self.authenticate_key(raw_current_key)
        if authenticated is None or authenticated[0].telegram_user_id != user_id:
            raise BillingError("current API Key verification failed")
        if action not in {"add", "reset", "revoke"}:
            raise BillingError("unsupported key action")
        with self.db.session() as session:
            if action != "add":
                target = session.get(APIKey, target_key_id)
                if target is None or target.current_owner_id != user_id or target.status != "active":
                    raise BillingError("target API Key is invalid")
            token = secure_token(24)
            session.add(KeyActionRequest(token_hash=hash_token(token, self.settings.session_secret), telegram_user_id=user_id, action=action,
                                         target_api_key_id=target_key_id, status="pending", created_at_ms=now_ms(),
                                         expires_at_ms=now_ms() + self.settings.action_ttl_seconds * 1000))
            return token

    def confirm_key_action(self, user_id: int, token: str) -> str:
        token_hash = hash_token(token, self.settings.session_secret)
        with self.db.session() as session:
            action = session.get(KeyActionRequest, token_hash)
            if action is None or action.telegram_user_id != user_id or action.status != "pending" or action.expires_at_ms <= now_ms():
                raise BillingError("confirmation token is invalid or expired")
            action_name, target_id = action.action, action.target_api_key_id
        new_raw: str | None = None
        if action_name in {"add", "reset"}:
            new_raw = generate_api_key(self.settings.api_key_prefix)
            self.cpa.add_key(new_raw)
        try:
            with self.db.session() as session:
                action = session.get(KeyActionRequest, token_hash)
                if action is None or action.status != "pending":
                    raise BillingError("action was already processed")
                if action_name in {"reset", "revoke"}:
                    target = session.get(APIKey, target_id)
                    if target is None or target.current_owner_id != user_id or target.status != "active":
                        raise BillingError("target API Key is no longer active")
                    self.cpa.remove_key_hash(target.cpamp_hash)
                    target.status, target.revoked_at_ms = "revoked", now_ms()
                    period = session.scalar(select(KeyOwnershipPeriod).where(KeyOwnershipPeriod.api_key_id == target.id, KeyOwnershipPeriod.valid_to_ms.is_(None)))
                    if period:
                        period.valid_to_ms = now_ms()
                    session.execute(update(WebSession).where(WebSession.api_key_id == target.id, WebSession.revoked_at_ms.is_(None)).values(revoked_at_ms=now_ms()))
                if new_raw:
                    new_key = self._insert_key(session, new_raw, user_id, f"web-{action_name}")
                    action.result_masked_key = new_key.masked_value
                action.status, action.confirmed_at_ms = "completed", now_ms()
                session.add(AuditLog(operator_type="telegram", operator_id=str(user_id), operation=f"key.{action_name}", target=str(target_id or "new"), created_at_ms=now_ms()))
        except Exception:
            if new_raw:
                self.cpa.remove_key_hash(cpamp_key_hash(new_raw))
            raise
        return new_raw or ""

    def _cycle(self, session: Any, name: str | None = None) -> BillingCycle | None:
        query = select(BillingCycle)
        if name:
            return session.scalar(query.where(BillingCycle.name == name))
        current = now_ms()
        return session.scalar(query.where(BillingCycle.start_at_ms <= current, BillingCycle.end_at_ms > current).order_by(BillingCycle.start_at_ms.desc()))

    def preview_cycle(self, cycle_name: str) -> list[Statement]:
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            if cycle is None:
                raise BillingError("billing cycle not found")
            if cycle.status == "closed":
                return list(session.scalars(select(Statement).where(Statement.cycle_id == cycle.id).order_by(Statement.amount_cents.desc())))
            unpriced = session.scalar(select(func.count()).select_from(RawUsageEvent).outerjoin(
                RatedEvent, and_(RatedEvent.raw_event_id == RawUsageEvent.id, RatedEvent.pricing_version_id == cycle.pricing_version_id)
            ).where(RawUsageEvent.occurred_at_ms >= cycle.start_at_ms, RawUsageEvent.occurred_at_ms < cycle.end_at_ms, RatedEvent.id.is_(None)))
            if unpriced:
                raise BillingError(f"cycle has {unpriced} unrated events")
            unassigned = session.scalar(select(func.count()).select_from(RatedEvent).where(
                RatedEvent.pricing_version_id == cycle.pricing_version_id, RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                RatedEvent.occurred_at_ms < cycle.end_at_ms, RatedEvent.pool_id.is_(None)))
            if unassigned:
                raise BillingError(f"cycle has {unassigned} unassigned events")
            rows = session.execute(select(RatedEvent.pool_id, RatedEvent.telegram_user_id, func.sum(RatedEvent.rated_weight_nano_usd)).where(
                RatedEvent.pricing_version_id == cycle.pricing_version_id, RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                RatedEvent.occurred_at_ms < cycle.end_at_ms, RatedEvent.telegram_user_id.is_not(None)
            ).group_by(RatedEvent.pool_id, RatedEvent.telegram_user_id)).all()
            pool_users: dict[int, dict[int, int]] = defaultdict(dict)
            for pool_id, user_id, weight in rows:
                pool_users[int(pool_id)][int(user_id)] = int(weight or 0)
            costs = {row.pool_id: row.fixed_cost_cents for row in session.scalars(select(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id))}
            tiers = parse_tiers(json.loads(cycle.tiers_json))
            lines: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
            for pool_id, users in pool_users.items():
                billed = {uid: tiered_weight(weight, tiers) for uid, weight in users.items()}
                allocated = largest_remainder(costs.get(pool_id, 0), billed)
                for uid, actual in users.items():
                    lines[uid].append((pool_id, actual, billed[uid], allocated[uid]))
            adjustments = defaultdict(int)
            for row in session.scalars(select(Adjustment).where(Adjustment.cycle_id == cycle.id)):
                adjustments[row.telegram_user_id] += row.amount_cents
            session.execute(delete(StatementLine).where(StatementLine.statement_id.in_(select(Statement.id).where(Statement.cycle_id == cycle.id))))
            session.execute(delete(Statement).where(Statement.cycle_id == cycle.id))
            for user_id, user_lines in lines.items():
                statement = Statement(cycle_id=cycle.id, telegram_user_id=user_id,
                                      actual_weight_nano_usd=sum(v[1] for v in user_lines), billed_weight_nano_usd=sum(v[2] for v in user_lines),
                                      amount_cents=sum(v[3] for v in user_lines) + adjustments[user_id], adjustment_cents=adjustments[user_id],
                                      generated_at_ms=now_ms(), final=False)
                session.add(statement)
                session.flush()
                for pool_id, actual, billed, amount in user_lines:
                    key_count = session.scalar(select(func.count(func.distinct(APIKey.id))).select_from(APIKey).where(APIKey.current_owner_id == user_id)) or 0
                    session.add(StatementLine(statement_id=statement.id, pool_id=pool_id, actual_weight_nano_usd=actual,
                                              billed_weight_nano_usd=billed, amount_cents=amount, api_key_count=int(key_count)))
            cycle.status = "preview"
            session.flush()
            return list(session.scalars(select(Statement).where(Statement.cycle_id == cycle.id).order_by(Statement.amount_cents.desc())))

    def close_cycle(self, cycle_name: str, operator_id: int, confirm_waiver: bool) -> None:
        self.preview_cycle(cycle_name)
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            if cycle is None or cycle.status == "closed":
                raise BillingError("cycle is missing or already closed")
            if cycle.data_quality_waiver and not confirm_waiver:
                raise BillingError("data quality waiver confirmation is required")
            cycle.status, cycle.closed_at_ms, cycle.closed_by = "closed", now_ms(), operator_id
            session.execute(update(Statement).where(Statement.cycle_id == cycle.id).values(final=True))
            session.add(AuditLog(operator_type="telegram", operator_id=str(operator_id), operation="cycle.close", target=cycle.name,
                                 after_json=json.dumps({"waiver": cycle.data_quality_waiver}), created_at_ms=now_ms()))

    def dashboard(self, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            users = {u.telegram_user_id: u for u in session.scalars(select(TelegramUser))}
            if cycle is None:
                return {"cycle": None, "rows": [], "totals": {}}
            statements = list(session.scalars(select(Statement).where(Statement.cycle_id == cycle.id).order_by(Statement.amount_cents.desc())))
            rows = []
            for statement in statements:
                user = users.get(statement.telegram_user_id)
                name = (f"@{user.username}" if user and user.username else " ".join(v for v in [user.first_name if user else None, user.last_name if user else None] if v)) or str(statement.telegram_user_id)
                rows.append({"telegram_user_id": statement.telegram_user_id, "name": name,
                             "actual": format_usd_nano(statement.actual_weight_nano_usd), "billed": format_usd_nano(statement.billed_weight_nano_usd),
                             "amount": format_cents(statement.amount_cents), "amount_cents": statement.amount_cents,
                             "key_count": session.scalar(select(func.count()).select_from(APIKey).where(APIKey.current_owner_id == statement.telegram_user_id, APIKey.status == "active")) or 0})
            return {"cycle": {"name": cycle.name, "status": cycle.status, "start_at_ms": cycle.start_at_ms, "end_at_ms": cycle.end_at_ms,
                              "waiver": cycle.data_quality_waiver}, "rows": rows,
                    "totals": {"actual": format_usd_nano(sum(s.actual_weight_nano_usd for s in statements)),
                               "billed": format_usd_nano(sum(s.billed_weight_nano_usd for s in statements)),
                               "amount": format_cents(sum(s.amount_cents for s in statements))}}

    def user_summary(self, user_id: int, cycle_name: str | None = None, include_keys: bool = False) -> dict[str, Any]:
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None:
                raise BillingError("user not found")
            cycle = self._cycle(session, cycle_name)
            statement = session.scalar(select(Statement).where(Statement.cycle_id == cycle.id, Statement.telegram_user_id == user_id)) if cycle else None
            data: dict[str, Any] = {"telegram_user_id": user_id, "username": user.username, "first_name": user.first_name, "last_name": user.last_name,
                                    "statement": None if statement is None else {"actual": format_usd_nano(statement.actual_weight_nano_usd),
                                    "billed": format_usd_nano(statement.billed_weight_nano_usd), "amount": format_cents(statement.amount_cents)}}
            if cycle:
                model_rows = session.execute(select(RawUsageEvent.model, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens),
                                                    func.sum(RatedEvent.rated_weight_nano_usd)).join(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)
                                             .where(RatedEvent.telegram_user_id == user_id, RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                                                    RatedEvent.occurred_at_ms < cycle.end_at_ms).group_by(RawUsageEvent.model)
                                             .order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc())).all()
                tier_rows = session.execute(select(RatedEvent.service_tier, func.count(RatedEvent.id), func.sum(RatedEvent.rated_weight_nano_usd))
                                            .where(RatedEvent.telegram_user_id == user_id, RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                                                   RatedEvent.occurred_at_ms < cycle.end_at_ms).group_by(RatedEvent.service_tier)).all()
                data["models"] = [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0), "cost": format_usd_nano(int(cost or 0))}
                                  for model, requests, tokens, cost in model_rows]
                data["tiers"] = [{"tier": tier, "requests": int(requests or 0), "cost": format_usd_nano(int(cost or 0))}
                                 for tier, requests, cost in tier_rows]
            if include_keys:
                data["keys"] = [{"id": key.id, "masked": key.masked_value, "name": key.display_name, "status": key.status}
                                for key in session.scalars(select(APIKey).where(APIKey.current_owner_id == user_id).order_by(APIKey.id))]
                if cycle:
                    events = session.execute(select(RawUsageEvent.timestamp, RawUsageEvent.model, RawUsageEvent.api_key_hash,
                                                    RawUsageEvent.input_tokens, RawUsageEvent.cached_tokens, RawUsageEvent.output_tokens,
                                                    RawUsageEvent.total_tokens, RawUsageEvent.failed, RatedEvent.rated_weight_nano_usd)
                                             .join(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)
                                             .where(RatedEvent.telegram_user_id == user_id, RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                                                    RatedEvent.occurred_at_ms < cycle.end_at_ms)
                                             .order_by(RatedEvent.occurred_at_ms.desc()).limit(100)).all()
                    key_names = {key.cpamp_hash: key.masked_value for key in session.scalars(select(APIKey).where(APIKey.current_owner_id == user_id))}
                    data["events"] = [{"timestamp": row[0], "model": row[1], "key": key_names.get(row[2], mask_hash(row[2] or "")),
                                       "input": row[3], "cached": row[4], "output": row[5], "total": row[6], "failed": bool(row[7]),
                                       "cost": format_usd_nano(int(row[8] or 0))} for row in events]
            return data

    def add_adjustment(self, cycle_name: str, user_id: int, amount_cents: int, reason: str, operator_id: int) -> None:
        if not reason.strip():
            raise BillingError("adjustment reason is required")
        with self.db.session() as session:
            cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == cycle_name))
            if cycle is None or cycle.status == "closed":
                raise BillingError("cycle is missing or closed")
            if session.get(TelegramUser, user_id) is None:
                raise BillingError("user not found")
            session.add(Adjustment(cycle_id=cycle.id, telegram_user_id=user_id, amount_cents=amount_cents, reason=reason,
                                   operator_user_id=operator_id, created_at_ms=now_ms()))
            session.add(AuditLog(operator_type="web", operator_id=str(operator_id), operation="adjustment.create",
                                 target=f"{cycle_name}:{user_id}", after_json=json.dumps({"amount_cents": amount_cents}), reason=reason, created_at_ms=now_ms()))

    def transfer_key(self, key_id: int, new_user_id: int, operator_id: int, reason: str, effective_at_ms: int | None = None) -> None:
        if not reason.strip():
            raise BillingError("transfer reason is required")
        effective = effective_at_ms or now_ms()
        with self.db.session() as session:
            key = session.get(APIKey, key_id)
            if key is None or session.get(TelegramUser, new_user_id) is None:
                raise BillingError("key or target user not found")
            old_user = key.current_owner_id
            period = session.scalar(select(KeyOwnershipPeriod).where(KeyOwnershipPeriod.api_key_id == key.id, KeyOwnershipPeriod.valid_to_ms.is_(None)))
            if period:
                if effective < period.valid_from_ms:
                    raise BillingError("effective time precedes current ownership")
                period.valid_to_ms = effective
            session.add(KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=new_user_id, valid_from_ms=effective,
                                           source="admin-transfer", reason=reason, operator_user_id=operator_id, created_at_ms=now_ms()))
            key.current_owner_id = new_user_id
            session.add(AuditLog(operator_type="web", operator_id=str(operator_id), operation="key.transfer", target=str(key_id),
                                 before_json=json.dumps({"owner": old_user}), after_json=json.dumps({"owner": new_user_id}), reason=reason, created_at_ms=now_ms()))

    def create_pool(self, name: str, auth_pattern: str | None, model_pattern: str | None, priority: int = 100) -> int:
        with self.db.session() as session:
            if session.scalar(select(ResourcePool).where(ResourcePool.name == name)):
                raise BillingError("pool already exists")
            pool = ResourcePool(name=name, active=True, created_at_ms=now_ms())
            session.add(pool); session.flush()
            session.add(PoolAssignmentRule(pool_id=pool.id, priority=priority, auth_index_pattern=auth_pattern or None,
                                           model_pattern=model_pattern or None, active=True))
            return pool.id

    def admin_snapshot(self) -> dict[str, Any]:
        with self.db.session() as session:
            return {
                "cycles": self.list_cycles(),
                "pools": [{"id": p.id, "name": p.name, "active": p.active} for p in session.scalars(select(ResourcePool).order_by(ResourcePool.id))],
                "pricing": [{"id": p.id, "name": p.name, "status": p.status, "source": p.source} for p in session.scalars(select(PricingVersion).order_by(PricingVersion.id.desc()))],
                "dead_letters": [{"id": d.id, "source_event_id": d.source_event_id, "error": d.error} for d in session.scalars(select(DeadLetter).where(DeadLetter.resolved_at_ms.is_(None)).limit(50))],
                "audits": [{"operation": a.operation, "target": a.target, "operator": a.operator_id, "reason": a.reason, "at": a.created_at_ms}
                           for a in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(50))],
            }

    def usage_summary(self) -> dict[str, Any]:
        with self.db.session() as session:
            total = session.execute(select(func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd)).select_from(RawUsageEvent).outerjoin(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)).one()
            cutoff = now_ms() - 86_400_000
            recent = session.execute(select(func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd)).select_from(RawUsageEvent).outerjoin(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id).where(RawUsageEvent.occurred_at_ms >= cutoff)).one()
            return {"total_requests": int(total[0] or 0), "total_tokens": int(total[1] or 0), "total_cost": format_usd_nano(int(total[2] or 0)),
                    "recent_requests": int(recent[0] or 0), "recent_tokens": int(recent[1] or 0), "recent_cost": format_usd_nano(int(recent[2] or 0))}

    def rankings(self, since_ms: int | None = None) -> list[dict[str, Any]]:
        with self.db.session() as session:
            query = select(RatedEvent.telegram_user_id, func.count(RatedEvent.id), func.sum(RawUsageEvent.total_tokens),
                           func.sum(RatedEvent.rated_weight_nano_usd)).join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
            if since_ms is not None:
                query = query.where(RatedEvent.occurred_at_ms >= since_ms)
            rows = query.group_by(RatedEvent.telegram_user_id).order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc()).all()
            users = {u.telegram_user_id: u for u in session.scalars(select(TelegramUser))}
            result = []
            for user_id, requests, tokens, cost in rows:
                if user_id is None:
                    name = "未绑定 Telegram 的 API Key"
                    key_count = session.scalar(select(func.count(func.distinct(RawUsageEvent.api_key_hash))).join(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id).where(RatedEvent.telegram_user_id.is_(None))) or 0
                else:
                    user = users.get(int(user_id))
                    name = f"@{user.username}" if user and user.username else str(user_id)
                    key_count = session.scalar(select(func.count()).select_from(APIKey).where(APIKey.current_owner_id == user_id)) or 0
                result.append({"telegram_user_id": user_id, "name": name, "requests": int(requests or 0), "tokens": int(tokens or 0),
                               "cost": format_usd_nano(int(cost or 0)), "key_count": int(key_count)})
            return result

    def hourly_usage(self, hours: int = 24) -> tuple[list[str], list[dict[str, Any]]]:
        start = now_ms() - hours * 3_600_000
        with self.db.session() as session:
            rows = session.execute(select(RatedEvent.telegram_user_id, RatedEvent.occurred_at_ms, RawUsageEvent.total_tokens)
                                   .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
                                   .where(RatedEvent.occurred_at_ms >= start)).all()
            users = {u.telegram_user_id: u for u in session.scalars(select(TelegramUser))}
        labels = [datetime.fromtimestamp((start + index * 3_600_000) / 1000, ZoneInfo(self.settings.timezone)).strftime("%m-%d %H") for index in range(hours + 1)]
        grouped: dict[int | None, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for user_id, occurred_at, tokens in rows:
            index = min(hours, max(0, int((int(occurred_at) - start) // 3_600_000)))
            grouped[user_id][labels[index]] += int(tokens or 0)
        series = []
        for user_id, values in grouped.items():
            user = users.get(user_id) if user_id is not None else None
            name = f"@{user.username}" if user and user.username else (str(user_id) if user_id is not None else "未绑定 Key")
            series.append({"name": name, "values": dict(values), "total": sum(values.values())})
        series.sort(key=lambda item: item["total"], reverse=True)
        return labels, series

    def model_usage(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(select(RawUsageEvent.model, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens),
                                          func.sum(RatedEvent.rated_weight_nano_usd)).outerjoin(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)
                                   .group_by(RawUsageEvent.model).order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc()).limit(limit)).all()
            return [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0), "cost": format_usd_nano(int(cost or 0))}
                    for model, requests, tokens, cost in rows]

    def account_usage(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as session:
            label = func.coalesce(func.nullif(RawUsageEvent.account_snapshot, ""), func.nullif(RawUsageEvent.source_label, ""), "-")
            rows = session.execute(select(label, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd))
                                   .outerjoin(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id).group_by(label)
                                   .order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc()).limit(limit)).all()
            result = []
            for name, requests, tokens, cost in rows:
                latest = session.scalar(select(RawUsageEvent).where(
                    or_(RawUsageEvent.account_snapshot == name, RawUsageEvent.source_label == name),
                    or_(RawUsageEvent.response_metadata_json.is_not(None), RawUsageEvent.quota_used_percent.is_not(None), RawUsageEvent.quota_recover_at_ms.is_not(None)),
                ).order_by(RawUsageEvent.occurred_at_ms.desc()))
                quota = "暂无"
                if latest:
                    parts = []
                    if latest.quota_used_percent is not None:
                        parts.append(f"used={Decimal(latest.quota_used_percent) / Decimal(1_000_000):.1f}%")
                    if latest.quota_plan_type:
                        parts.append(f"plan={latest.quota_plan_type}")
                    if latest.quota_recover_at_ms:
                        parts.append(f"reset={latest.quota_recover_at_ms}")
                    if latest.response_metadata_json:
                        try:
                            metadata = json.loads(latest.response_metadata_json)
                            primary = metadata.get("x-codex-primary-used-percent") or metadata.get("x-codex-primary-used-percentage")
                            secondary = metadata.get("x-codex-secondary-used-percent") or metadata.get("x-codex-secondary-used-percentage")
                            if primary is not None: parts.append(f"5h={primary}%")
                            if secondary is not None: parts.append(f"week={secondary}%")
                        except (TypeError, json.JSONDecodeError):
                            pass
                    quota = " ".join(parts) or "暂无"
                result.append({"name": name, "requests": int(requests or 0), "tokens": int(tokens or 0), "cost": format_usd_nano(int(cost or 0)), "quota": quota})
            return result

    def set_manual_allowed(self, user_id: int, allowed: bool) -> None:
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None:
                user = TelegramUser(telegram_user_id=user_id, last_seen_at_ms=now_ms(), is_admin=user_id in self.settings.admin_user_ids)
                session.add(user)
            user.manual_allowed = allowed

    def set_allowed_chat(self, chat_id: int, note: str | None) -> None:
        with self.db.session() as session:
            row = session.get(AllowedChat, chat_id)
            if row is None:
                session.add(AllowedChat(chat_id=chat_id, note=note, updated_at_ms=now_ms()))
            else:
                row.note, row.updated_at_ms = note, now_ms()

    def remove_allowed_chat(self, chat_id: int) -> None:
        with self.db.session() as session:
            session.execute(delete(AllowedChat).where(AllowedChat.chat_id == chat_id))

    def list_allowed_chats(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return [{"chat_id": row.chat_id, "note": row.note} for row in session.scalars(select(AllowedChat).order_by(AllowedChat.chat_id))]

    def chat_is_allowed(self, chat_id: int) -> bool:
        with self.db.session() as session:
            return session.get(AllowedChat, chat_id) is not None

    def name_unowned_key(self, key_or_hash: str, name: str) -> None:
        key_hash = key_or_hash.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", key_or_hash) else cpamp_key_hash(key_or_hash)
        with self.db.session() as session:
            key = session.scalar(select(APIKey).where(APIKey.cpamp_hash == key_hash))
            if key and key.current_owner_id is not None:
                raise BillingError("key is or was owned by a Telegram user")
            if key is None:
                key = APIKey(cpamp_hash=key_hash, login_fingerprint=None, masked_value=mask_hash(key_hash), status="unowned", created_at_ms=now_ms())
                session.add(key)
            key.display_name = name[:120]

    def update_cycle_time(self, name: str, start: str, end: str) -> None:
        zone = ZoneInfo(self.settings.timezone)
        start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=zone)
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=zone)
        if end_dt <= start_dt:
            raise BillingError("cycle end must be after start")
        with self.db.session() as session:
            cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == name))
            if cycle is None or cycle.status == "closed":
                raise BillingError("cycle is missing or closed")
            cycle.start_at_ms, cycle.end_at_ms = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    def list_cycles(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return [{"name": c.name, "start_at_ms": c.start_at_ms, "end_at_ms": c.end_at_ms, "status": c.status,
                     "waiver": c.data_quality_waiver} for c in session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc()))]

    def telegram_key_action(self, user_id: int, action_name: str, target_id: int) -> str:
        token = secure_token(24)
        with self.db.session() as session:
            target = session.get(APIKey, target_id)
            if target is None or target.current_owner_id != user_id or target.status != "active":
                raise BillingError("target API Key is invalid")
            session.add(KeyActionRequest(token_hash=hash_token(token, self.settings.session_secret), telegram_user_id=user_id,
                                         action=action_name, target_api_key_id=target_id, status="pending", created_at_ms=now_ms(),
                                         expires_at_ms=now_ms() + self.settings.action_ttl_seconds * 1000))
        return self.confirm_key_action(user_id, token)

    def revoke_user(self, user_id: int) -> int:
        keys = self.active_keys(user_id)
        for key in keys:
            self.telegram_key_action(user_id, "revoke", key.id)
        self.set_manual_allowed(user_id, False)
        return len(keys)

    def list_users(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            users = session.scalars(select(TelegramUser).order_by(TelegramUser.last_seen_at_ms.desc())).all()
            return [{"id": user.telegram_user_id, "username": user.username or "-", "registered": bool(user.registered_at_ms),
                     "keys": session.scalar(select(func.count()).select_from(APIKey).where(APIKey.current_owner_id == user.telegram_user_id, APIKey.status == "active")) or 0}
                    for user in users]

    def reconciliation(self, cycle_name: str | None = None) -> dict[str, Any]:
        with self._cpamp() as cpamp:
            source_count = int(cpamp.execute("select count(*) from usage_events").fetchone()[0])
        with self.db.session() as session:
            raw_count = session.scalar(select(func.count()).select_from(RawUsageEvent)) or 0
            rated_count = session.scalar(select(func.count()).select_from(RatedEvent)) or 0
            dead = session.scalar(select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved_at_ms.is_(None))) or 0
            unowned = session.scalar(select(func.count()).select_from(RatedEvent).where(RatedEvent.telegram_user_id.is_(None))) or 0
            unassigned = session.scalar(select(func.count()).select_from(RatedEvent).where(RatedEvent.pool_id.is_(None))) or 0
            result = {"cpamp_events": source_count, "raw_events": raw_count, "rated_events": rated_count, "dead_letters": dead,
                      "unowned_events": unowned, "unassigned_events": unassigned, "ok": source_count == raw_count and dead == 0}
            session.add(ReconciliationRun(cycle_id=None, result_json=json.dumps(result), ok=bool(result["ok"]), created_at_ms=now_ms()))
            return result

    def create_cycle(self, name: str, start: str, end: str, fixed_cost_cents: int, waiver: str | None = None) -> None:
        zone = ZoneInfo(self.settings.timezone)
        start_ms = int(datetime.fromisoformat(start).replace(tzinfo=zone).timestamp() * 1000) if datetime.fromisoformat(start).tzinfo is None else int(datetime.fromisoformat(start).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(end).replace(tzinfo=zone).timestamp() * 1000) if datetime.fromisoformat(end).tzinfo is None else int(datetime.fromisoformat(end).timestamp() * 1000)
        if end_ms <= start_ms:
            raise BillingError("cycle end must be after start")
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            cycle = BillingCycle(name=name, start_at_ms=start_ms, end_at_ms=end_ms, timezone=self.settings.timezone, status="open",
                                 pricing_version_id=version_id, tiers_json=json.dumps(DEFAULT_TIERS), data_quality_waiver=waiver, created_at_ms=now_ms())
            session.add(cycle)
            session.flush()
            pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
            if pool is None:
                raise BillingError("default pool is missing")
            session.add(CyclePoolCost(cycle_id=cycle.id, pool_id=pool.id, fixed_cost_cents=fixed_cost_cents))
