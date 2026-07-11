from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .database import Database, now_ms
from .domain import NANO_USD, format_cents, format_usd_nano, largest_remainder, parse_tiers, tiered_weight
from .models import (
    APIKey,
    AdminWebSession,
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

LOGGER = logging.getLogger(__name__)


class BillingError(RuntimeError):
    pass


class BillingDependencyError(BillingError):
    pass


class CPAClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.cpa_base_url
        self.key = settings.cpa_management_key
        self.lock_path = settings.database_path.parent / "cpa-api-keys.lock"

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

    @contextmanager
    def _key_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="ascii") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _put_keys(self, keys: list[str]) -> None:
        self._request("PUT", "/v0/management/api-keys", json=keys)

    def add_key(self, raw_key: str) -> None:
        with self._key_lock():
            keys = self.list_keys()
            if any(constant_equal(key, raw_key) for key in keys):
                return
            self._put_keys(keys + [raw_key])

    def remove_key_hash(self, key_hash: str) -> str | None:
        with self._key_lock():
            keys = self.list_keys()
            removed = next((key for key in keys if constant_equal(cpamp_key_hash(key), key_hash)), None)
            if removed is None:
                return None
            self._put_keys([key for key in keys if not constant_equal(cpamp_key_hash(key), key_hash)])
            return removed

    def replace_key_hash(self, key_hash: str, new_raw_key: str) -> str:
        with self._key_lock():
            keys = self.list_keys()
            replaced: str | None = None
            updated: list[str] = []
            for key in keys:
                if replaced is None and constant_equal(cpamp_key_hash(key), key_hash):
                    replaced = key
                    updated.append(new_raw_key)
                else:
                    updated.append(key)
            if replaced is None:
                raise BillingError("target API Key is no longer active in CPA")
            self._put_keys(updated)
            return replaced

    def health(self) -> dict[str, Any]:
        started = time.monotonic()
        keys = self.list_keys()
        return {
            "reachable": True,
            "api_key_count": len(keys),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


class KeeperClient:
    REQUEST_HEADER = {"X-CPA-Usage-Keeper-Request": "fetch"}

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.keeper_base_url
        self.password = settings.keeper_login_password

    @contextmanager
    def session(self) -> Iterator[httpx.Client]:
        if not self.password:
            raise BillingDependencyError("Keeper 登录密码未配置")
        with httpx.Client(timeout=httpx.Timeout(20, connect=5)) as client:
            response = client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"password": self.password},
                headers=self.REQUEST_HEADER,
            )
            response.raise_for_status()
            yield client

    def request(self, client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self.REQUEST_HEADER)
        response = client.request(method, f"{self.base_url}/api/v1{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def status_snapshot(self, range_name: str, window: str, start: str | None, end: str | None) -> dict[str, Any]:
        overview_params: dict[str, str] = {"range": range_name}
        if start:
            overview_params["start"] = start
        if end:
            overview_params["end"] = end
        with self.session() as client:
            try:
                update_status = self.request(client, "GET", "/update/check")
            except httpx.HTTPError:
                update_status = {"available": False, "error": "Keeper 更新检查不可用"}
            return {
                "status": self.request(client, "GET", "/status"),
                "version": self.request(client, "GET", "/version"),
                "update": update_status,
                "overview": self.request(client, "GET", "/usage/overview", params=overview_params),
                "realtime": self.request(client, "GET", "/usage/overview/realtime", params={"window": window}),
            }

    def accounts_snapshot(self) -> dict[str, Any]:
        with self.session() as client:
            identities = self.request(client, "GET", "/usage/identities")
            items = identities.get("identities", []) if isinstance(identities, dict) else []
            auth_indexes = [str(item.get("identity")) for item in items if item.get("identity")]
            quota = {"items": []}
            if auth_indexes:
                quota = self.request(client, "POST", "/quota/cache", json={"auth_indexes": auth_indexes})
            inspection = self.request(client, "GET", "/quota/inspection")
            return {"identities": items, "quota": quota, "inspection": inspection}

    def pulse(self) -> dict[str, Any]:
        with self.session() as client:
            return {
                "status": self.request(client, "GET", "/status"),
                "inspection": self.request(client, "GET", "/quota/inspection"),
            }

    def refresh(self, auth_indexes: list[str]) -> Any:
        with self.session() as client:
            return self.request(client, "POST", "/quota/refresh", json={"auth_indexes": auth_indexes})

    def refresh_status(self, auth_index: str) -> Any:
        with self.session() as client:
            return self.request(client, "GET", f"/quota/refresh/{auth_index}")


def _nano_per_token(value: Any) -> int:
    return int((Decimal(str(value or 0)) * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))


def _ppm(value: Decimal) -> int:
    return int((value * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


class BillingService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database
        self.cpa = CPAClient(settings)
        self.keeper = KeeperClient(settings)

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

    def import_cpamp_prices(self, name: str, operator_type: str | None = None, operator_id: str | None = None,
                            allow_existing: bool = True) -> int:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
            raise BillingError("pricing version name must use letters, numbers, dot, underscore, or hyphen")
        with self.db.session() as session:
            existing = session.scalar(select(PricingVersion).where(PricingVersion.name == name))
            if existing is not None:
                if allow_existing:
                    return existing.id
                raise BillingError("pricing version already exists")
        with self._cpamp() as source_db:
            rows = source_db.execute("select * from model_prices order by model").fetchall()
        if not rows:
            raise BillingError("CPAMP model_prices is empty")
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
            if operator_type and operator_id:
                session.add(AuditLog(operator_type=operator_type, operator_id=operator_id, operation="pricing.import",
                                     target=name, after_json=json.dumps({"models": len(rows)}), created_at_ms=now_ms()))
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
            session.execute(delete(WebSession).where(WebSession.expires_at_ms <= now_ms()))
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
            key = session.get(APIKey, row.api_key_id)
            if user is None or key is None or key.status != "active" or key.current_owner_id != user.telegram_user_id:
                row.revoked_at_ms = now_ms()
                return None
            return row, user

    def revoke_session(self, token: str) -> None:
        with self.db.session() as session:
            row = session.get(WebSession, hash_token(token, self.settings.session_secret))
            if row:
                row.revoked_at_ms = now_ms()

    def authenticate_admin_token(self, raw_token: str) -> bool:
        return constant_equal(raw_token.strip(), self.settings.admin_token)

    def _admin_credential_fingerprint(self) -> str:
        return hash_token(self.settings.admin_token, self.settings.session_secret)

    def create_admin_session(self) -> tuple[str, str]:
        token, csrf = secure_token(), secure_token(18)
        current = now_ms()
        with self.db.session() as session:
            session.execute(delete(AdminWebSession).where(AdminWebSession.expires_at_ms <= current))
            session.add(AdminWebSession(
                session_hash=hash_token(token, self.settings.session_secret),
                credential_fingerprint=self._admin_credential_fingerprint(),
                csrf_token=csrf,
                created_at_ms=current,
                expires_at_ms=current + self.settings.session_ttl_seconds * 1000,
            ))
        return token, csrf

    def get_admin_session(self, token: str | None) -> AdminWebSession | None:
        if not token:
            return None
        with self.db.session() as session:
            row = session.get(AdminWebSession, hash_token(token, self.settings.session_secret))
            if row is None or row.revoked_at_ms is not None or row.expires_at_ms <= now_ms():
                return None
            if not constant_equal(row.credential_fingerprint, self._admin_credential_fingerprint()):
                row.revoked_at_ms = now_ms()
                return None
            return row

    def revoke_admin_session(self, token: str) -> None:
        with self.db.session() as session:
            row = session.get(AdminWebSession, hash_token(token, self.settings.session_secret))
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

    def execute_web_key_action(self, user_id: int, raw_current_key: str, action: str,
                               target_key_id: int | None) -> dict[str, Any]:
        authenticated = self.authenticate_key(raw_current_key)
        if authenticated is None or authenticated[0].telegram_user_id != user_id:
            raise BillingError("当前 API Key 验证失败")
        return self._execute_key_action(user_id, action, target_key_id, "web-user", str(user_id))

    def _execute_key_action(self, user_id: int, action_name: str, target_id: int | None,
                            operator_type: str, operator_id: str) -> dict[str, Any]:
        if action_name not in {"add", "reset", "revoke"}:
            raise BillingError("不支持的 Key 操作")
        target_hash: str | None = None
        if action_name != "add":
            with self.db.session() as session:
                target = session.get(APIKey, target_id)
                if target is None or target.current_owner_id != user_id or target.status != "active":
                    raise BillingError("目标 API Key 无效或已吊销")
                target_hash = target.cpamp_hash

        new_raw = generate_api_key(self.settings.api_key_prefix) if action_name in {"add", "reset"} else None
        removed_raw: str | None = None
        try:
            if action_name == "add" and new_raw:
                self.cpa.add_key(new_raw)
            elif action_name == "reset" and new_raw and target_hash:
                removed_raw = self.cpa.replace_key_hash(target_hash, new_raw)
            elif action_name == "revoke" and target_hash:
                removed_raw = self.cpa.remove_key_hash(target_hash)
                if removed_raw is None:
                    raise BillingError("目标 API Key 已不在 CPA 有效列表中")

            new_key_id: int | None = None
            with self.db.session() as session:
                target = None
                if action_name != "add":
                    target = session.get(APIKey, target_id)
                    if target is None or target.current_owner_id != user_id or target.status != "active":
                        raise BillingError("目标 API Key 状态已变化，请刷新后重试")
                    target.status = "revoked"
                    target.revoked_at_ms = now_ms()
                    period = session.scalar(select(KeyOwnershipPeriod).where(
                        KeyOwnershipPeriod.api_key_id == target.id,
                        KeyOwnershipPeriod.valid_to_ms.is_(None),
                    ))
                    if period:
                        period.valid_to_ms = now_ms()
                    session.execute(update(WebSession).where(
                        WebSession.api_key_id == target.id,
                        WebSession.revoked_at_ms.is_(None),
                    ).values(revoked_at_ms=now_ms()))
                if new_raw:
                    new_key = self._insert_key(session, new_raw, user_id, f"{operator_type}-{action_name}")
                    new_key_id = new_key.id
                session.add(AuditLog(
                    operator_type=operator_type,
                    operator_id=operator_id,
                    operation=f"key.{action_name}",
                    target=str(target_id or "new"),
                    after_json=json.dumps({"new_key_id": new_key_id}) if new_key_id else None,
                    created_at_ms=now_ms(),
                ))
            return {
                "action": action_name,
                "target_key_id": target_id,
                "new_api_key": new_raw,
                "new_key_id": new_key_id,
            }
        except Exception:
            try:
                if action_name == "add" and new_raw:
                    self.cpa.remove_key_hash(cpamp_key_hash(new_raw))
                elif action_name == "reset" and new_raw and removed_raw:
                    self.cpa.replace_key_hash(cpamp_key_hash(new_raw), removed_raw)
                elif action_name == "revoke" and removed_raw:
                    self.cpa.add_key(removed_raw)
            except Exception:
                LOGGER.exception("CPA API Key rollback failed after %s", action_name)
            raise

    def confirm_key_action(self, user_id: int, token: str) -> str:
        token_hash = hash_token(token, self.settings.session_secret)
        with self.db.session() as session:
            claimed = session.execute(
                update(KeyActionRequest)
                .where(
                    KeyActionRequest.token_hash == token_hash,
                    KeyActionRequest.telegram_user_id == user_id,
                    KeyActionRequest.status == "pending",
                    KeyActionRequest.expires_at_ms > now_ms(),
                )
                .values(status="processing")
            )
            if claimed.rowcount != 1:
                raise BillingError("confirmation token is invalid or expired")
            action = session.get(KeyActionRequest, token_hash)
            action_name, target_id = action.action, action.target_api_key_id
        try:
            result = self._execute_key_action(user_id, action_name, target_id, "telegram", str(user_id))
        except Exception:
            with self.db.session() as session:
                action = session.get(KeyActionRequest, token_hash)
                if action is not None and action.status == "processing" and action.expires_at_ms > now_ms():
                    action.status = "pending"
            raise
        with self.db.session() as session:
            action = session.get(KeyActionRequest, token_hash)
            if action is None or action.status != "processing":
                raise BillingError("action was already processed")
            action.status = "completed"
            action.confirmed_at_ms = now_ms()
            if result["new_api_key"]:
                action.result_masked_key = mask_api_key(result["new_api_key"])
        return str(result["new_api_key"] or "")

    def rename_key(self, user_id: int, key_id: int, name: str | None) -> dict[str, Any]:
        normalized = (name or "").strip()
        if len(normalized) > 120:
            raise BillingError("Key 名称不能超过 120 个字符")
        with self.db.session() as session:
            key = session.get(APIKey, key_id)
            if key is None or key.current_owner_id != user_id:
                raise BillingError("API Key 不存在或不属于当前用户")
            before = key.display_name
            key.display_name = normalized or None
            session.add(AuditLog(
                operator_type="web-user",
                operator_id=str(user_id),
                operation="key.rename",
                target=str(key_id),
                before_json=json.dumps({"name": before}, ensure_ascii=False),
                after_json=json.dumps({"name": key.display_name}, ensure_ascii=False),
                created_at_ms=now_ms(),
            ))
            return {"id": key.id, "name": key.display_name, "masked": key.masked_value, "status": key.status}

    def _cycle(self, session: Any, name: str | None = None) -> BillingCycle | None:
        query = select(BillingCycle)
        if name:
            return session.scalar(query.where(BillingCycle.name == name))
        current = now_ms()
        return session.scalar(query.where(BillingCycle.start_at_ms <= current, BillingCycle.end_at_ms > current).order_by(BillingCycle.start_at_ms.desc()))

    def _display_cycle(self, session: Any, name: str | None = None) -> BillingCycle | None:
        cycle = self._cycle(session, name)
        if name and cycle is None:
            raise BillingError("billing cycle not found")
        if cycle is not None:
            return cycle
        return session.scalar(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc()))

    def _format_timestamp(self, value: int | None) -> str:
        if value is None:
            return "-"
        return datetime.fromtimestamp(value / 1000, ZoneInfo(self.settings.timezone)).strftime("%Y-%m-%d %H:%M")

    def _iso_timestamp(self, value: int | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, ZoneInfo(self.settings.timezone)).isoformat()

    def _parse_filter_time(self, value: str | None, *, end_of_date: bool = False) -> int | None:
        if not value:
            return None
        normalized = value.strip()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BillingError("时间格式无效") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.settings.timezone))
        if end_of_date and len(normalized) == 10:
            parsed += timedelta(days=1)
        return int(parsed.timestamp() * 1000)

    @staticmethod
    def _usd_filter_to_nano(value: str | None) -> int | None:
        if value is None or not value.strip():
            return None
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise BillingError("成本筛选值无效") from exc
        if not amount.is_finite() or amount < 0:
            raise BillingError("成本筛选值必须是非负数")
        return int((amount * NANO_USD).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _user_name(user: TelegramUser | None, fallback: int | str) -> str:
        if user and user.username:
            return f"@{user.username}"
        if user:
            full_name = " ".join(value for value in (user.first_name, user.last_name) if value)
            if full_name:
                return full_name
        return str(fallback)

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
            for pool_id in costs.keys() | pool_users.keys():
                users = pool_users.get(pool_id, {})
                billed = {uid: tiered_weight(weight, tiers) for uid, weight in users.items()}
                if costs.get(pool_id, 0) and not any(weight > 0 for weight in billed.values()):
                    raise BillingError(f"pool {pool_id} has fixed cost but no billable Telegram usage")
                allocated = largest_remainder(costs.get(pool_id, 0), billed)
                for uid, actual in users.items():
                    lines[uid].append((pool_id, actual, billed[uid], allocated[uid]))
            adjustments = defaultdict(int)
            for row in session.scalars(select(Adjustment).where(Adjustment.cycle_id == cycle.id)):
                adjustments[row.telegram_user_id] += row.amount_cents
            session.execute(delete(StatementLine).where(StatementLine.statement_id.in_(select(Statement.id).where(Statement.cycle_id == cycle.id))))
            session.execute(delete(Statement).where(Statement.cycle_id == cycle.id))
            for user_id in lines.keys() | adjustments.keys():
                user_lines = lines.get(user_id, [])
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

    def close_cycle(self, cycle_name: str, operator_id: int | None, confirm_waiver: bool,
                    operator_type: str = "telegram") -> None:
        self.preview_cycle(cycle_name)
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            if cycle is None or cycle.status == "closed":
                raise BillingError("cycle is missing or already closed")
            if cycle.data_quality_waiver and not confirm_waiver:
                raise BillingError("data quality waiver confirmation is required")
            cycle.status, cycle.closed_at_ms, cycle.closed_by = "closed", now_ms(), operator_id
            session.execute(update(Statement).where(Statement.cycle_id == cycle.id).values(final=True))
            session.add(AuditLog(operator_type=operator_type, operator_id=str(operator_id if operator_id is not None else "admin-token"),
                                 operation="cycle.close", target=cycle.name,
                                 after_json=json.dumps({"waiver": cycle.data_quality_waiver}), created_at_ms=now_ms()))

    def dashboard(self, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            cycles = list(session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc())))
            if cycle_name and cycle is None:
                raise BillingError("billing cycle not found")
            if cycle is None:
                return {"cycle": None, "cycles": [{"name": item.name, "status": item.status} for item in cycles],
                        "rows": [], "models": [],
                        "totals": {"requests": 0, "tokens": 0, "actual": "0.0000", "billed": "0.0000", "amount": "0.00"}}

            period = (
                RatedEvent.pricing_version_id == cycle.pricing_version_id,
                RatedEvent.occurred_at_ms >= cycle.start_at_ms,
                RatedEvent.occurred_at_ms < cycle.end_at_ms,
            )
            usage_rows = session.execute(
                select(RatedEvent.telegram_user_id, func.count(RatedEvent.id), func.sum(RawUsageEvent.total_tokens),
                       func.sum(RatedEvent.rated_weight_nano_usd))
                .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
                .where(*period)
                .group_by(RatedEvent.telegram_user_id)
            ).all()
            usage = {user_id: (int(requests or 0), int(tokens or 0), int(cost or 0))
                     for user_id, requests, tokens, cost in usage_rows}
            statements = {row.telegram_user_id: row for row in session.scalars(
                select(Statement).where(Statement.cycle_id == cycle.id)
            )}
            key_counts = {owner_id: int(count or 0) for owner_id, count in session.execute(
                select(APIKey.current_owner_id, func.count(APIKey.id))
                .where(APIKey.status == "active", APIKey.current_owner_id.is_not(None))
                .group_by(APIKey.current_owner_id)
            )}
            users = list(session.scalars(
                select(TelegramUser).where(TelegramUser.registered_at_ms.is_not(None)).order_by(TelegramUser.telegram_user_id)
            ))
            rows: list[dict[str, Any]] = []
            for user in users:
                requests, tokens, actual = usage.get(user.telegram_user_id, (0, 0, 0))
                statement = statements.get(user.telegram_user_id)
                rows.append({
                    "telegram_user_id": user.telegram_user_id,
                    "name": self._user_name(user, user.telegram_user_id),
                    "requests": requests,
                    "tokens": tokens,
                    "actual": format_usd_nano(actual),
                    "actual_nano": actual,
                    "billed": format_usd_nano(statement.billed_weight_nano_usd if statement else 0),
                    "amount": format_cents(statement.amount_cents if statement else 0),
                    "amount_cents": statement.amount_cents if statement else 0,
                    "key_count": key_counts.get(user.telegram_user_id, 0),
                    "unowned": False,
                })
            if None in usage:
                requests, tokens, actual = usage[None]
                unowned_key_count = session.scalar(
                    select(func.count(func.distinct(RawUsageEvent.api_key_hash)))
                    .select_from(RatedEvent)
                    .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
                    .where(*period, RatedEvent.telegram_user_id.is_(None))
                ) or 0
                rows.append({
                    "telegram_user_id": None,
                    "name": "未绑定 Telegram 的 API Key",
                    "requests": requests,
                    "tokens": tokens,
                    "actual": format_usd_nano(actual),
                    "actual_nano": actual,
                    "billed": "0.0000",
                    "amount": "0.00",
                    "amount_cents": 0,
                    "key_count": int(unowned_key_count),
                    "unowned": True,
                })
            rows.sort(key=lambda item: (item["actual_nano"], item["requests"]), reverse=True)
            model_rows = session.execute(
                select(RawUsageEvent.model, func.count(RatedEvent.id), func.sum(RawUsageEvent.total_tokens),
                       func.sum(RatedEvent.rated_weight_nano_usd))
                .join(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)
                .where(*period)
                .group_by(RawUsageEvent.model)
                .order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc())
                .limit(20)
            ).all()
            return {
                "cycle": {"name": cycle.name, "status": cycle.status, "start": self._format_timestamp(cycle.start_at_ms),
                          "end": self._format_timestamp(cycle.end_at_ms), "waiver": cycle.data_quality_waiver},
                "cycles": [{"name": item.name, "status": item.status} for item in cycles],
                "rows": rows,
                "models": [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0),
                            "cost": format_usd_nano(int(cost or 0))} for model, requests, tokens, cost in model_rows],
                "totals": {
                    "requests": sum(item["requests"] for item in rows),
                    "tokens": sum(item["tokens"] for item in rows),
                    "actual": format_usd_nano(sum(item["actual_nano"] for item in rows)),
                    "billed": format_usd_nano(sum(item.billed_weight_nano_usd for item in statements.values())),
                    "amount": format_cents(sum(item.amount_cents for item in statements.values())),
                },
            }

    def user_summary(self, user_id: int, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None or user.registered_at_ms is None:
                raise BillingError("user not found")
            cycle = self._display_cycle(session, cycle_name)
            statement = session.scalar(select(Statement).where(Statement.cycle_id == cycle.id, Statement.telegram_user_id == user_id)) if cycle else None
            data: dict[str, Any] = {"telegram_user_id": user_id, "username": user.username, "first_name": user.first_name, "last_name": user.last_name,
                                    "statement": None if statement is None else {"actual": format_usd_nano(statement.actual_weight_nano_usd),
                                    "billed": format_usd_nano(statement.billed_weight_nano_usd), "amount": format_cents(statement.amount_cents)},
                                    "cycle": None, "cycles": [], "summary": {"requests": 0, "tokens": 0, "cost": "0.0000",
                                    "failed": 0, "success_rate": "-", "long_context": 0}, "models": [], "tiers": []}
            data["cycles"] = [{"name": item.name, "status": item.status} for item in session.scalars(
                select(BillingCycle).order_by(BillingCycle.start_at_ms.desc())
            )]
            if cycle:
                data["cycle"] = {"name": cycle.name, "status": cycle.status, "start": self._format_timestamp(cycle.start_at_ms),
                                 "end": self._format_timestamp(cycle.end_at_ms)}
                period = (RatedEvent.pricing_version_id == cycle.pricing_version_id,
                          RatedEvent.occurred_at_ms >= cycle.start_at_ms, RatedEvent.occurred_at_ms < cycle.end_at_ms,
                          RatedEvent.telegram_user_id == user_id)
                summary = session.execute(
                    select(func.count(RatedEvent.id), func.sum(RawUsageEvent.total_tokens),
                           func.sum(RatedEvent.rated_weight_nano_usd), func.sum(RawUsageEvent.failed),
                           func.sum(RatedEvent.long_context_applied))
                    .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
                    .where(*period)
                ).one()
                requests = int(summary[0] or 0)
                failed = int(summary[3] or 0)
                data["summary"] = {"requests": requests, "tokens": int(summary[1] or 0),
                                   "cost": format_usd_nano(int(summary[2] or 0)), "failed": failed,
                                   "success_rate": f"{(requests - failed) * 100 / requests:.1f}%" if requests else "-",
                                   "long_context": int(summary[4] or 0)}
                model_rows = session.execute(select(RawUsageEvent.model, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens),
                                                    func.sum(RatedEvent.rated_weight_nano_usd)).join(RatedEvent, RatedEvent.raw_event_id == RawUsageEvent.id)
                                             .where(*period).group_by(RawUsageEvent.model)
                                             .order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc())).all()
                tier_rows = session.execute(select(RatedEvent.service_tier, func.count(RatedEvent.id), func.sum(RatedEvent.rated_weight_nano_usd))
                                            .where(*period).group_by(RatedEvent.service_tier)).all()
                data["models"] = [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0), "cost": format_usd_nano(int(cost or 0))}
                                  for model, requests, tokens, cost in model_rows]
                data["tiers"] = [{"tier": tier, "requests": int(requests or 0), "cost": format_usd_nano(int(cost or 0))}
                                 for tier, requests, cost in tier_rows]
            return data

    def user_keys(self, user_id: int) -> dict[str, Any]:
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None or user.registered_at_ms is None:
                raise BillingError("用户不存在")
            keys = list(session.scalars(
                select(APIKey).where(APIKey.current_owner_id == user_id).order_by(APIKey.id.desc())
            ))
            return {
                "telegram_user_id": user_id,
                "keys": [{
                    "id": key.id,
                    "masked": key.masked_value,
                    "name": key.display_name,
                    "status": key.status,
                    "created_at": self._iso_timestamp(key.created_at_ms),
                    "revoked_at": self._iso_timestamp(key.revoked_at_ms),
                } for key in keys],
            }

    def request_filter_options(self, user_id: int) -> dict[str, Any]:
        event_ownership = (
            RawUsageEvent.__table__
            .join(APIKey.__table__, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
            .join(KeyOwnershipPeriod.__table__, and_(
                KeyOwnershipPeriod.api_key_id == APIKey.id,
                KeyOwnershipPeriod.valid_from_ms <= RawUsageEvent.occurred_at_ms,
                or_(
                    KeyOwnershipPeriod.valid_to_ms.is_(None),
                    KeyOwnershipPeriod.valid_to_ms > RawUsageEvent.occurred_at_ms,
                ),
            ))
        )
        ownership_filter = KeyOwnershipPeriod.telegram_user_id == user_id
        with self.db.session() as session:
            if session.get(TelegramUser, user_id) is None:
                raise BillingError("用户不存在")
            models = [str(value) for value in session.scalars(
                select(RawUsageEvent.model).select_from(event_ownership)
                .where(ownership_filter).distinct().order_by(RawUsageEvent.model)
            ) if value]
            tiers = sorted({str(value or "default") for value in session.scalars(
                select(RawUsageEvent.service_tier).select_from(event_ownership)
                .where(ownership_filter).distinct().order_by(RawUsageEvent.service_tier)
            )})
            providers = [str(value) for value in session.scalars(
                select(RawUsageEvent.provider).select_from(event_ownership)
                .where(ownership_filter, RawUsageEvent.provider.is_not(None))
                .distinct().order_by(RawUsageEvent.provider)
            )]
            failure_codes = [int(value) for value in session.scalars(
                select(RawUsageEvent.fail_status_code).select_from(event_ownership)
                .where(ownership_filter, RawUsageEvent.fail_status_code.is_not(None))
                .distinct().order_by(RawUsageEvent.fail_status_code)
            )]
            bounds = session.execute(
                select(func.min(RawUsageEvent.occurred_at_ms), func.max(RawUsageEvent.occurred_at_ms))
                .select_from(event_ownership).where(ownership_filter)
            ).one()
            key_rows = session.execute(
                select(APIKey.id, APIKey.masked_value, APIKey.display_name, APIKey.status)
                .join(KeyOwnershipPeriod, KeyOwnershipPeriod.api_key_id == APIKey.id)
                .where(KeyOwnershipPeriod.telegram_user_id == user_id)
                .distinct().order_by(APIKey.id.desc())
            ).all()
            return {
                "models": models,
                "tiers": tiers,
                "providers": providers,
                "failure_codes": failure_codes,
                "keys": [{"id": row[0], "masked": row[1], "name": row[2], "status": row[3]} for row in key_rows],
                "range": {"start": self._iso_timestamp(bounds[0]), "end": self._iso_timestamp(bounds[1])},
            }

    def request_history(
        self,
        user_id: int,
        *,
        start: str | None = None,
        end: str | None = None,
        models: list[str] | None = None,
        tier: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        key_id: int | None = None,
        failure_code: int | None = None,
        min_tokens: int | None = None,
        max_tokens: int | None = None,
        min_cost: str | None = None,
        max_cost: str | None = None,
        min_latency: int | None = None,
        max_latency: int | None = None,
        min_ttft: int | None = None,
        max_ttft: int | None = None,
        long_context: bool | None = None,
        query_text: str | None = None,
        sort: str = "time_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 100:
            raise BillingError("分页参数无效")
        for minimum, maximum, label in (
            (min_tokens, max_tokens, "Token"),
            (min_latency, max_latency, "延迟"),
            (min_ttft, max_ttft, "TTFT"),
        ):
            if minimum is not None and minimum < 0 or maximum is not None and maximum < 0:
                raise BillingError(f"{label} 筛选值必须是非负数")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise BillingError(f"{label} 筛选下限不能大于上限")
        since_ms = self._parse_filter_time(start)
        until_ms = self._parse_filter_time(end, end_of_date=True)
        if since_ms is not None and until_ms is not None and since_ms >= until_ms:
            raise BillingError("结束时间必须晚于开始时间")
        min_cost_nano = self._usd_filter_to_nano(min_cost)
        max_cost_nano = self._usd_filter_to_nano(max_cost)
        if min_cost_nano is not None and max_cost_nano is not None and min_cost_nano > max_cost_nano:
            raise BillingError("成本筛选下限不能大于上限")

        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            event_history = (
                RawUsageEvent.__table__
                .join(APIKey.__table__, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
                .join(KeyOwnershipPeriod.__table__, and_(
                    KeyOwnershipPeriod.api_key_id == APIKey.id,
                    KeyOwnershipPeriod.valid_from_ms <= RawUsageEvent.occurred_at_ms,
                    or_(
                        KeyOwnershipPeriod.valid_to_ms.is_(None),
                        KeyOwnershipPeriod.valid_to_ms > RawUsageEvent.occurred_at_ms,
                    ),
                ))
                .outerjoin(RatedEvent.__table__, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ))
            )
            filters: list[Any] = [KeyOwnershipPeriod.telegram_user_id == user_id]
            if since_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms >= since_ms)
            if until_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms < until_ms)
            if models:
                filters.append(RawUsageEvent.model.in_([value for value in models if value]))
            if tier:
                normalized_tier = tier.lower()
                if normalized_tier == "default":
                    filters.append(or_(RawUsageEvent.service_tier.is_(None), func.lower(RawUsageEvent.service_tier) == "default"))
                else:
                    filters.append(func.lower(RawUsageEvent.service_tier) == normalized_tier)
            if provider:
                filters.append(RawUsageEvent.provider == provider)
            if status == "success":
                filters.append(RawUsageEvent.failed.is_(False))
            elif status == "failed":
                filters.append(RawUsageEvent.failed.is_(True))
            elif status == "priced":
                filters.append(RatedEvent.id.is_not(None))
            elif status == "unpriced":
                filters.append(RatedEvent.id.is_(None))
            elif status not in {None, "", "all"}:
                raise BillingError("请求状态筛选值无效")
            if key_id is not None:
                filters.append(APIKey.id == key_id)
            if failure_code is not None:
                filters.append(RawUsageEvent.fail_status_code == failure_code)
            if min_tokens is not None:
                filters.append(RawUsageEvent.total_tokens >= min_tokens)
            if max_tokens is not None:
                filters.append(RawUsageEvent.total_tokens <= max_tokens)
            if min_cost_nano is not None:
                filters.append(RatedEvent.rated_weight_nano_usd >= min_cost_nano)
            if max_cost_nano is not None:
                filters.append(RatedEvent.rated_weight_nano_usd <= max_cost_nano)
            if min_latency is not None:
                filters.append(RawUsageEvent.latency_ms >= min_latency)
            if max_latency is not None:
                filters.append(RawUsageEvent.latency_ms <= max_latency)
            if min_ttft is not None:
                filters.append(RawUsageEvent.ttft_ms >= min_ttft)
            if max_ttft is not None:
                filters.append(RawUsageEvent.ttft_ms <= max_ttft)
            if long_context is not None:
                filters.append(RatedEvent.long_context_applied.is_(long_context))
            if query_text and query_text.strip():
                escaped = query_text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                filters.append(or_(
                    RawUsageEvent.request_id.ilike(pattern, escape="\\"),
                    RawUsageEvent.model.ilike(pattern, escape="\\"),
                    RawUsageEvent.resolved_model.ilike(pattern, escape="\\"),
                ))

            sort_options = {
                "time_desc": RawUsageEvent.occurred_at_ms.desc(),
                "time_asc": RawUsageEvent.occurred_at_ms.asc(),
                "tokens_desc": RawUsageEvent.total_tokens.desc(),
                "cost_desc": RatedEvent.rated_weight_nano_usd.desc(),
                "latency_desc": RawUsageEvent.latency_ms.desc(),
                "ttft_desc": RawUsageEvent.ttft_ms.desc(),
            }
            if sort not in sort_options:
                raise BillingError("排序方式无效")

            aggregate = session.execute(
                select(
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                    func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    func.sum(case((RatedEvent.id.is_(None), 1), else_=0)),
                ).select_from(event_history).where(*filters)
            ).one()
            total = int(aggregate[0] or 0)
            rows = session.execute(
                select(RawUsageEvent, RatedEvent, APIKey)
                .select_from(event_history)
                .where(*filters)
                .order_by(sort_options[sort], RawUsageEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            items = []
            for event, rated, key in rows:
                items.append({
                    "id": event.id,
                    "request_id": event.request_id,
                    "occurred_at_ms": event.occurred_at_ms,
                    "occurred_at": self._iso_timestamp(event.occurred_at_ms),
                    "provider": event.provider,
                    "model": event.model,
                    "requested_model": event.requested_model,
                    "resolved_model": event.resolved_model,
                    "service_tier": rated.service_tier if rated else (event.service_tier or "default"),
                    "key": {"id": key.id, "masked": key.masked_value, "name": key.display_name},
                    "tokens": {
                        "input": event.input_tokens,
                        "cached": max(event.cached_tokens, event.cache_read_tokens),
                        "cache_creation": event.cache_creation_tokens,
                        "output": event.output_tokens,
                        "reasoning": event.reasoning_tokens,
                        "total": event.total_tokens,
                    },
                    "failed": bool(event.failed),
                    "status_code": event.fail_status_code,
                    "latency_ms": event.latency_ms,
                    "ttft_ms": event.ttft_ms,
                    "long_context": bool(rated.long_context_applied) if rated else None,
                    "cost_nano_usd": rated.rated_weight_nano_usd if rated else None,
                    "cost": format_usd_nano(rated.rated_weight_nano_usd) if rated else None,
                    "pricing_status": "priced" if rated else "unpriced",
                })
            return {
                "items": items,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size,
                },
                "summary": {
                    "requests": total,
                    "tokens": int(aggregate[1] or 0),
                    "cost_nano_usd": int(aggregate[2] or 0),
                    "cost": format_usd_nano(int(aggregate[2] or 0)),
                    "failed": int(aggregate[3] or 0),
                    "unpriced": int(aggregate[4] or 0),
                },
            }

    def ranking_snapshot(self, range_name: str, start: str | None = None, end: str | None = None,
                         cycle_name: str | None = None, sort: str = "cost") -> dict[str, Any]:
        current = now_ms()
        since_ms: int | None
        until_ms: int | None
        selected_cycle: BillingCycle | None = None
        with self.db.session() as session:
            if range_name == "24h":
                since_ms, until_ms = current - 86_400_000, current
                version_id = self._active_pricing_id(session)
            elif range_name == "7d":
                since_ms, until_ms = current - 7 * 86_400_000, current
                version_id = self._active_pricing_id(session)
            elif range_name == "30d":
                since_ms, until_ms = current - 30 * 86_400_000, current
                version_id = self._active_pricing_id(session)
            elif range_name == "all":
                since_ms, until_ms = None, current
                version_id = self._active_pricing_id(session)
            elif range_name == "cycle":
                selected_cycle = self._display_cycle(session, cycle_name)
                if selected_cycle is None:
                    raise BillingError("尚未创建账期")
                since_ms, until_ms = selected_cycle.start_at_ms, selected_cycle.end_at_ms
                version_id = selected_cycle.pricing_version_id
            elif range_name == "custom":
                since_ms = self._parse_filter_time(start)
                until_ms = self._parse_filter_time(end, end_of_date=True)
                if since_ms is None or until_ms is None or since_ms >= until_ms:
                    raise BillingError("自定义排行需要有效的开始和结束时间")
                version_id = self._active_pricing_id(session)
            else:
                raise BillingError("排行时间范围无效")

            ranking_source = (
                RawUsageEvent.__table__
                .outerjoin(APIKey.__table__, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
                .outerjoin(KeyOwnershipPeriod.__table__, and_(
                    KeyOwnershipPeriod.api_key_id == APIKey.id,
                    KeyOwnershipPeriod.valid_from_ms <= RawUsageEvent.occurred_at_ms,
                    or_(
                        KeyOwnershipPeriod.valid_to_ms.is_(None),
                        KeyOwnershipPeriod.valid_to_ms > RawUsageEvent.occurred_at_ms,
                    ),
                ))
                .outerjoin(RatedEvent.__table__, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ))
            )
            filters: list[Any] = []
            if since_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms >= since_ms)
            if until_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms < until_ms)
            usage_rows = session.execute(
                select(
                    KeyOwnershipPeriod.telegram_user_id,
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                    func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    func.sum(case((RatedEvent.long_context_applied.is_(True), 1), else_=0)),
                )
                .select_from(ranking_source)
                .where(*filters)
                .group_by(KeyOwnershipPeriod.telegram_user_id)
            ).all()
            usage = {
                user_id: {
                    "requests": int(requests or 0),
                    "tokens": int(tokens or 0),
                    "cost_nano_usd": int(cost or 0),
                    "failed": int(failed or 0),
                    "long_context": int(long_context or 0),
                }
                for user_id, requests, tokens, cost, failed, long_context in usage_rows
            }
            key_counts = {owner_id: int(count or 0) for owner_id, count in session.execute(
                select(APIKey.current_owner_id, func.count(APIKey.id))
                .where(APIKey.current_owner_id.is_not(None), APIKey.status == "active")
                .group_by(APIKey.current_owner_id)
            )}
            rows: list[dict[str, Any]] = []
            users = list(session.scalars(
                select(TelegramUser).where(TelegramUser.registered_at_ms.is_not(None))
            ))
            for user in users:
                values = usage.get(user.telegram_user_id, {
                    "requests": 0, "tokens": 0, "cost_nano_usd": 0, "failed": 0, "long_context": 0,
                })
                requests = values["requests"]
                failed = values["failed"]
                rows.append({
                    "telegram_user_id": user.telegram_user_id,
                    "name": self._user_name(user, user.telegram_user_id),
                    **values,
                    "cost": format_usd_nano(values["cost_nano_usd"]),
                    "success_rate": round((requests - failed) * 100 / requests, 2) if requests else None,
                    "key_count": key_counts.get(user.telegram_user_id, 0),
                    "unowned": False,
                })
            if None in usage:
                values = usage[None]
                requests = values["requests"]
                failed = values["failed"]
                rows.append({
                    "telegram_user_id": None,
                    "name": "未绑定 Telegram 的 API Key",
                    **values,
                    "cost": format_usd_nano(values["cost_nano_usd"]),
                    "success_rate": round((requests - failed) * 100 / requests, 2) if requests else None,
                    "key_count": int(session.scalar(
                        select(func.count(func.distinct(RawUsageEvent.api_key_hash)))
                        .select_from(ranking_source)
                        .where(*filters, KeyOwnershipPeriod.telegram_user_id.is_(None))
                    ) or 0),
                    "unowned": True,
                })
            sort_fields = {
                "cost": "cost_nano_usd",
                "tokens": "tokens",
                "requests": "requests",
                "failures": "failed",
            }
            if sort not in sort_fields:
                raise BillingError("排行排序方式无效")
            rows.sort(key=lambda item: (item[sort_fields[sort]], item["requests"]), reverse=True)
            return {
                "range": {
                    "name": range_name,
                    "start": self._iso_timestamp(since_ms),
                    "end": self._iso_timestamp(until_ms),
                    "cycle": selected_cycle.name if selected_cycle else None,
                },
                "sort": sort,
                "rows": rows,
                "totals": {
                    "requests": sum(item["requests"] for item in rows),
                    "tokens": sum(item["tokens"] for item in rows),
                    "cost_nano_usd": sum(item["cost_nano_usd"] for item in rows),
                    "cost": format_usd_nano(sum(item["cost_nano_usd"] for item in rows)),
                    "failed": sum(item["failed"] for item in rows),
                },
            }

    @staticmethod
    def _price_rate(rate: int | None) -> dict[str, Any] | None:
        if rate is None:
            return None
        per_million = Decimal(rate) / Decimal(1000)
        return {
            "nano_usd_per_token": rate,
            "usd_per_million": format(per_million.normalize(), "f"),
        }

    def pricing_snapshot(self, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            version = session.get(PricingVersion, version_id)
            cycle = self._display_cycle(session, cycle_name)
            if cycle_name and cycle is None:
                raise BillingError("账期不存在")
            rules = list(session.scalars(
                select(ModelPriceRule)
                .where(ModelPriceRule.pricing_version_id == version_id)
                .order_by(ModelPriceRule.model)
            ))
            cycles = list(session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc())))
            pools = list(session.scalars(select(ResourcePool).order_by(ResourcePool.id)))
            assignments = list(session.scalars(
                select(PoolAssignmentRule).order_by(PoolAssignmentRule.priority, PoolAssignmentRule.id)
            ))
            costs = {}
            if cycle:
                costs = {row.pool_id: row.fixed_cost_cents for row in session.scalars(
                    select(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id)
                )}
            tiers = json.loads(cycle.tiers_json) if cycle else DEFAULT_TIERS
            unpriced = int(session.scalar(
                select(func.count()).select_from(RawUsageEvent)
                .outerjoin(RatedEvent, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ))
                .where(RatedEvent.id.is_(None))
            ) or 0)
            return {
                "active_version": {
                    "id": version.id,
                    "name": version.name,
                    "source": version.source,
                    "activated_at": self._iso_timestamp(version.activated_at_ms),
                    "unpriced_events": unpriced,
                },
                "models": [{
                    "model": rule.model,
                    "default": {
                        "input": self._price_rate(rule.input_nano_per_token),
                        "output": self._price_rate(rule.output_nano_per_token),
                        "cache_read": self._price_rate(rule.cache_read_nano_per_token),
                        "cache_creation": self._price_rate(rule.cache_creation_nano_per_token),
                    },
                    "priority": {
                        "input": self._price_rate(rule.priority_input_nano_per_token),
                        "output": self._price_rate(rule.priority_output_nano_per_token),
                        "cache_read": self._price_rate(rule.priority_cache_read_nano_per_token),
                        "cache_creation": self._price_rate(rule.priority_cache_creation_nano_per_token),
                    },
                    "flex": {
                        "input": self._price_rate(rule.flex_input_nano_per_token),
                        "output": self._price_rate(rule.flex_output_nano_per_token),
                    },
                    "long_context": {
                        "threshold_tokens": rule.long_threshold_tokens,
                        "input_multiplier_ppm": rule.long_input_multiplier_ppm,
                        "output_multiplier_ppm": rule.long_output_multiplier_ppm,
                    },
                } for rule in rules],
                "billing": {
                    "cycles": [{"name": item.name, "status": item.status} for item in cycles],
                    "cycle": None if cycle is None else {
                        "name": cycle.name,
                        "status": cycle.status,
                        "start": self._iso_timestamp(cycle.start_at_ms),
                        "end": self._iso_timestamp(cycle.end_at_ms),
                        "waiver": cycle.data_quality_waiver,
                    },
                    "tiers": [{
                        "left_usd": str(item["left"]),
                        "right_usd": None if item.get("right") is None else str(item["right"]),
                        "multiplier": str(item["multiplier"]),
                    } for item in tiers],
                    "pools": [{
                        "id": pool.id,
                        "name": pool.name,
                        "active": pool.active,
                        "fixed_cost_cents": int(costs.get(pool.id, 0)),
                        "fixed_cost": format_cents(int(costs.get(pool.id, 0))),
                        "rules": [{
                            "priority": assignment.priority,
                            "account_scope": "restricted" if assignment.auth_index_pattern else "all",
                            "model_pattern": assignment.model_pattern,
                            "active": assignment.active,
                        } for assignment in assignments if assignment.pool_id == pool.id],
                    } for pool in pools],
                    "semantics": {
                        "cached_is_input_subset": True,
                        "reasoning_is_output_subset": True,
                        "long_context_uses_total_input": True,
                        "unowned_keys_are_billed": False,
                        "allocation_method": "largest_remainder",
                    },
                },
            }

    @staticmethod
    def _quota_rows(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
        if not isinstance(payload, dict):
            return [], None
        rows = []
        for item in payload.get("quota", []) if isinstance(payload.get("quota"), list) else []:
            if not isinstance(item, dict):
                continue
            window = item.get("window") if isinstance(item.get("window"), dict) else {}
            rows.append({
                "key": item.get("key"),
                "label": item.get("label"),
                "scope": item.get("scope"),
                "metric": item.get("metric"),
                "plan_type": item.get("planType"),
                "used_percent": item.get("usedPercent"),
                "allowed": item.get("allowed"),
                "limit_reached": item.get("limitReached"),
                "window_seconds": window.get("seconds"),
                "reset_at": item.get("resetAt"),
                "reset_after_seconds": item.get("resetAfterSeconds"),
                "window_usage_tokens": item.get("window_usage_tokens"),
                "window_usage_cost": item.get("window_usage_cost"),
            })
        credits = payload.get("rateLimitResetCreditsAvailableCount")
        return rows, int(credits) if isinstance(credits, (int, float)) else None

    def _sanitize_accounts(self, raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
        identities = raw.get("identities", []) if isinstance(raw, dict) else []
        quota_items = raw.get("quota", {}).get("items", []) if isinstance(raw.get("quota"), dict) else []
        quota_by_auth = {
            str(item.get("auth_index")): item
            for item in quota_items
            if isinstance(item, dict) and item.get("auth_index")
        }
        account_by_auth: dict[str, str] = {}
        auth_by_account: dict[str, str] = {}
        accounts: list[dict[str, Any]] = []
        for identity in identities if isinstance(identities, list) else []:
            if not isinstance(identity, dict):
                continue
            account_id = str(identity.get("id") or "").strip()
            auth_index = str(identity.get("identity") or "").strip()
            if not account_id or not auth_index:
                continue
            account_by_auth[auth_index] = account_id
            auth_by_account[account_id] = auth_index
            quota_item = quota_by_auth.get(auth_index, {})
            quota_rows, credits = self._quota_rows(quota_item.get("quota"))
            health = identity.get("credential_health") if isinstance(identity.get("credential_health"), dict) else None
            total_requests = int(identity.get("total_requests") or 0)
            success_count = int(identity.get("success_count") or 0)
            accounts.append({
                "id": account_id,
                "name": identity.get("alias") or identity.get("displayName") or identity.get("name") or f"上游账号 {account_id}",
                "type": identity.get("type"),
                "provider": identity.get("provider"),
                "auth_type": identity.get("auth_type_name"),
                "plan_type": identity.get("plan_type"),
                "disabled": bool(identity.get("disabled")),
                "active_start": identity.get("active_start"),
                "active_until": identity.get("active_until"),
                "usage": {
                    "requests": total_requests,
                    "success": success_count,
                    "failed": int(identity.get("failure_count") or 0),
                    "success_rate": round(success_count * 100 / total_requests, 2) if total_requests else None,
                    "input_tokens": int(identity.get("input_tokens") or 0),
                    "output_tokens": int(identity.get("output_tokens") or 0),
                    "reasoning_tokens": int(identity.get("reasoning_tokens") or 0),
                    "cached_tokens": int(identity.get("cached_tokens") or 0),
                    "total_tokens": int(identity.get("total_tokens") or 0),
                    "first_used_at": identity.get("first_used_at"),
                    "last_used_at": identity.get("last_used_at"),
                    "stats_updated_at": identity.get("stats_updated_at"),
                },
                "health": None if health is None else {
                    "window_seconds": health.get("window_seconds"),
                    "total_success": health.get("total_success"),
                    "total_failure": health.get("total_failure"),
                    "success_rate": health.get("success_rate"),
                    "window_start": health.get("window_start"),
                    "window_end": health.get("window_end"),
                    "buckets": health.get("buckets", []),
                },
                "quota": quota_rows,
                "quota_status": quota_item.get("status") or "unknown",
                "quota_refreshed_at": quota_item.get("refreshed_at"),
                "quota_http_status": quota_item.get("http_status_code"),
                "reset_credits_available": credits,
                "can_refresh": not bool(identity.get("disabled")),
            })
        accounts.sort(key=lambda item: (item["disabled"], str(item["name"]).casefold()))
        return accounts, account_by_auth, auth_by_account

    def accounts_snapshot(self) -> dict[str, Any]:
        try:
            raw = self.keeper.accounts_snapshot()
        except httpx.HTTPError as exc:
            raise BillingDependencyError("Keeper 上游账号服务不可用") from exc
        accounts, account_by_auth, _ = self._sanitize_accounts(raw)
        inspection_raw = raw.get("inspection", {}) if isinstance(raw, dict) else {}
        inspection_results = []
        for item in inspection_raw.get("results", []) if isinstance(inspection_raw, dict) else []:
            if not isinstance(item, dict):
                continue
            account_id = account_by_auth.get(str(item.get("auth_index") or ""))
            if account_id:
                inspection_results.append({
                    "account_id": account_id,
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "status": item.get("status"),
                    "refreshed_at": item.get("refreshed_at"),
                })
        inspection = {
            key: inspection_raw.get(key)
            for key in (
                "total", "cached", "running", "completed", "normal", "limit_reached",
                "unauthorized_401", "payment_required_402", "unauthorized_401_402",
                "other_failed", "unknown",
            )
        } if isinstance(inspection_raw, dict) else {}
        inspection["results"] = inspection_results
        return {"accounts": accounts, "inspection": inspection}

    def refresh_account_quotas(self, account_ids: list[str]) -> dict[str, Any]:
        try:
            raw_accounts = self.keeper.accounts_snapshot()
            accounts, account_by_auth, auth_by_account = self._sanitize_accounts(raw_accounts)
            refreshable_ids = {item["id"] for item in accounts if item["can_refresh"]}
            selected_ids = account_ids or sorted(refreshable_ids)
            unknown = sorted(set(selected_ids) - refreshable_ids)
            if unknown:
                raise BillingError("上游账号不存在或已失效")
            auth_indexes = [auth_by_account[account_id] for account_id in selected_ids]
            if not auth_indexes:
                raise BillingError("没有可刷新的上游账号")
            raw = self.keeper.refresh(auth_indexes)
        except httpx.HTTPError as exc:
            raise BillingDependencyError("Keeper 额度刷新请求失败") from exc
        tasks = []
        for item in raw.get("tasks", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            account_id = account_by_auth.get(str(item.get("authIndex") or ""))
            if account_id:
                tasks.append({"account_id": account_id, "status": "queued"})
        rejected = []
        for item in raw.get("rejected", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            account_id = account_by_auth.get(str(item.get("authIndex") or ""))
            if account_id:
                rejected.append({"account_id": account_id, "error": "额度刷新未被 Keeper 接受"})
        return {
            "tasks": tasks,
            "rejected": rejected,
            "accepted": int(raw.get("accepted") or 0) if isinstance(raw, dict) else 0,
            "skipped": int(raw.get("skipped") or 0) if isinstance(raw, dict) else 0,
            "limit": int(raw.get("limit") or 0) if isinstance(raw, dict) else 0,
        }

    def account_quota_refresh_status(self, account_id: str) -> dict[str, Any]:
        try:
            raw_accounts = self.keeper.accounts_snapshot()
            _, _, auth_by_account = self._sanitize_accounts(raw_accounts)
            auth_index = auth_by_account.get(account_id)
            if not auth_index:
                raise BillingError("上游账号不存在或已失效")
            raw = self.keeper.refresh_status(auth_index)
        except httpx.HTTPError as exc:
            raise BillingDependencyError("Keeper 额度刷新状态不可用") from exc
        quota, credits = self._quota_rows(raw.get("quota") if isinstance(raw, dict) else None)
        return {
            "account_id": account_id,
            "status": raw.get("status") if isinstance(raw, dict) else "unknown",
            "refreshed_at": raw.get("refreshed_at") if isinstance(raw, dict) else None,
            "expires_at": raw.get("expiresAt") if isinstance(raw, dict) else None,
            "http_status": raw.get("http_status_code") if isinstance(raw, dict) else None,
            "quota": quota,
            "reset_credits_available": credits,
        }

    @staticmethod
    def _sanitize_realtime(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        current_usage = raw.get("current_usage") if isinstance(raw.get("current_usage"), dict) else {}
        api_key_rows = current_usage.get("api_keys", []) if isinstance(current_usage.get("api_keys"), list) else []
        api_key_aggregate = {
            "count": len(api_key_rows),
            "requests": sum(int(item.get("requests") or 0) for item in api_key_rows if isinstance(item, dict)),
            "tokens": sum(int(item.get("tokens") or 0) for item in api_key_rows if isinstance(item, dict)),
            "cost": sum(float(item.get("cost") or 0) for item in api_key_rows if isinstance(item, dict)),
        }
        def safe_usage_rows(value: Any) -> list[dict[str, Any]]:
            result = []
            for item in value if isinstance(value, list) else []:
                if isinstance(item, dict):
                    result.append({
                        "label": item.get("label"),
                        "requests": item.get("requests"),
                        "tokens": item.get("tokens"),
                        "cost": item.get("cost"),
                        "share": item.get("share"),
                    })
            return result

        auth_files = safe_usage_rows(current_usage.get("auth_files"))
        distributions = {}
        raw_distributions = raw.get("response_distribution") if isinstance(raw.get("response_distribution"), dict) else {}
        for name in ("ttft", "latency"):
            item = raw_distributions.get(name) if isinstance(raw_distributions.get(name), dict) else {}
            distributions[name] = {
                "average_line": item.get("average_line", []),
                "total_particles": item.get("total_particles", 0),
                "sampled": item.get("sampled", False),
                "max_particles": item.get("max_particles", 0),
            }
        return {
            "window": raw.get("window"),
            "timezone": raw.get("timezone"),
            "bucket_seconds": raw.get("bucket_seconds"),
            "window_start": raw.get("window_start"),
            "window_end": raw.get("window_end"),
            "token_velocity": raw.get("token_velocity", []),
            "response_level": raw.get("response_level", []),
            "request_level": raw.get("request_level", []),
            "cache_level": raw.get("cache_level", []),
            "response_distribution": distributions,
            "current_usage": {
                "models": safe_usage_rows(current_usage.get("models")),
                "api_keys": api_key_aggregate,
                "upstream_accounts": auth_files,
                "ai_providers": safe_usage_rows(current_usage.get("ai_providers")),
            },
        }

    def _local_sync_status(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return [{
                "source": source.name,
                "last_event_id": checkpoint.last_event_id,
                "last_event_at": self._iso_timestamp(checkpoint.last_event_at_ms),
                "last_success_at": self._iso_timestamp(checkpoint.last_success_at_ms),
                "backlog": checkpoint.backlog,
                "last_error": checkpoint.last_error,
            } for source, checkpoint in session.execute(
                select(CPAMPSource, SyncCheckpoint).join(
                    SyncCheckpoint, SyncCheckpoint.source_id == CPAMPSource.id
                )
            )]

    def site_pulse(self) -> dict[str, Any]:
        keeper: dict[str, Any]
        cpa: dict[str, Any]
        try:
            raw = self.keeper.pulse()
            status = raw.get("status", {}) if isinstance(raw, dict) else {}
            inspection = raw.get("inspection", {}) if isinstance(raw, dict) else {}
            keeper = {
                "available": True,
                "running": status.get("running"),
                "sync_running": status.get("sync_running"),
                "last_run_at": status.get("last_run_at"),
                "last_status": status.get("last_status"),
                "quota_normal": inspection.get("normal"),
                "quota_total": inspection.get("total"),
                "quota_limit_reached": inspection.get("limit_reached"),
                "quota_failed": sum(int(inspection.get(key) or 0) for key in (
                    "unauthorized_401_402", "other_failed", "unknown",
                )),
            }
        except (httpx.HTTPError, BillingDependencyError):
            keeper = {"available": False, "error": "Keeper 不可用"}
        try:
            cpa = self.cpa.health()
        except (httpx.HTTPError, BillingError):
            cpa = {"reachable": False, "error": "CPA 管理接口不可用"}
        sync = self._local_sync_status()
        return {
            "generated_at": self._iso_timestamp(now_ms()),
            "cpa": cpa,
            "keeper": keeper,
            "worker": {
                "healthy": bool(sync) and all(not item["last_error"] for item in sync),
                "backlog": sum(int(item["backlog"] or 0) for item in sync),
                "sources": sync,
            },
        }

    def site_status(self, range_name: str = "24h", window: str = "60m",
                    start: str | None = None, end: str | None = None) -> dict[str, Any]:
        allowed_ranges = {"today", "yesterday", "24h", "7d", "30d", "custom"}
        allowed_windows = {"15m", "30m", "45m", "60m"}
        if range_name not in allowed_ranges:
            raise BillingError("全站状态时间范围无效")
        if window not in allowed_windows:
            raise BillingError("实时窗口无效")
        if range_name == "custom" and (not start or not end):
            raise BillingError("自定义状态范围需要开始和结束时间")
        keeper: dict[str, Any]
        errors: list[str] = []
        try:
            raw = self.keeper.status_snapshot(range_name, window, start, end)
            keeper = {
                "available": True,
                "status": raw.get("status", {}),
                "version": raw.get("version", {}),
                "update": raw.get("update", {}),
                "overview": raw.get("overview", {}),
                "realtime": self._sanitize_realtime(raw.get("realtime")),
            }
        except (httpx.HTTPError, BillingDependencyError):
            keeper = {"available": False, "error": "Keeper 状态接口不可用"}
            errors.append("keeper")
        try:
            cpa = self.cpa.health()
        except (httpx.HTTPError, BillingError):
            cpa = {"reachable": False, "error": "CPA 管理接口不可用"}
            errors.append("cpa")
        try:
            reconciliation = self.reconciliation()
        except (sqlite3.Error, BillingError):
            reconciliation = {"ok": False, "error": "CPAMP 对账不可用"}
            errors.append("reconciliation")
        return {
            "generated_at": self._iso_timestamp(now_ms()),
            "degraded": bool(errors),
            "errors": errors,
            "cpa": cpa,
            "keeper": keeper,
            "billing": {
                "sync": self._local_sync_status(),
                "usage": self.usage_summary(),
                "reconciliation": reconciliation,
            },
        }

    def add_adjustment(self, cycle_name: str, user_id: int, amount_cents: int, reason: str, operator_id: int | None,
                       operator_type: str = "telegram") -> None:
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
            session.add(AuditLog(operator_type=operator_type, operator_id=str(operator_id if operator_id is not None else "admin-token"),
                                 operation="adjustment.create",
                                 target=f"{cycle_name}:{user_id}", after_json=json.dumps({"amount_cents": amount_cents}), reason=reason, created_at_ms=now_ms()))

    def transfer_key(self, key_id: int, new_user_id: int, operator_id: int | None, reason: str,
                     effective_at_ms: int | None = None, operator_type: str = "telegram") -> None:
        if not reason.strip():
            raise BillingError("transfer reason is required")
        effective = effective_at_ms or now_ms()
        with self.db.session() as session:
            key = session.get(APIKey, key_id)
            if key is None or session.get(TelegramUser, new_user_id) is None:
                raise BillingError("key or target user not found")
            old_user = key.current_owner_id
            if old_user == new_user_id:
                raise BillingError("key already belongs to this user")
            period = session.scalar(select(KeyOwnershipPeriod).where(KeyOwnershipPeriod.api_key_id == key.id, KeyOwnershipPeriod.valid_to_ms.is_(None)))
            if period:
                if effective < period.valid_from_ms:
                    raise BillingError("effective time precedes current ownership")
                period.valid_to_ms = effective
            session.add(KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=new_user_id, valid_from_ms=effective,
                                           source="admin-transfer", reason=reason, operator_user_id=operator_id, created_at_ms=now_ms()))
            key.current_owner_id = new_user_id
            session.add(AuditLog(operator_type=operator_type, operator_id=str(operator_id if operator_id is not None else "admin-token"),
                                 operation="key.transfer", target=str(key_id),
                                 before_json=json.dumps({"owner": old_user}), after_json=json.dumps({"owner": new_user_id}), reason=reason, created_at_ms=now_ms()))

    def create_pool(self, name: str, auth_pattern: str | None, model_pattern: str | None, priority: int = 100,
                    operator_type: str | None = None, operator_id: str | None = None) -> int:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
            raise BillingError("pool name must use letters, numbers, dot, underscore, or hyphen")
        for pattern in (auth_pattern, model_pattern):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise BillingError(f"invalid pool regular expression: {exc}") from exc
        with self.db.session() as session:
            if session.scalar(select(ResourcePool).where(ResourcePool.name == name)):
                raise BillingError("pool already exists")
            pool = ResourcePool(name=name, active=True, created_at_ms=now_ms())
            session.add(pool); session.flush()
            session.add(PoolAssignmentRule(pool_id=pool.id, priority=priority, auth_index_pattern=auth_pattern or None,
                                           model_pattern=model_pattern or None, active=True))
            if operator_type and operator_id:
                session.add(AuditLog(operator_type=operator_type, operator_id=operator_id, operation="pool.create", target=name,
                                     after_json=json.dumps({"priority": priority, "auth_pattern": auth_pattern,
                                                            "model_pattern": model_pattern}), created_at_ms=now_ms()))
            return pool.id

    def admin_snapshot(self) -> dict[str, Any]:
        with self.db.session() as session:
            users = {user.telegram_user_id: user for user in session.scalars(select(TelegramUser))}
            keys = list(session.scalars(select(APIKey).order_by(APIKey.id)))
            key_map = {key.id: key for key in keys}
            cycles = list(session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc())))
            costs = {cycle_id: int(total or 0) for cycle_id, total in session.execute(
                select(CyclePoolCost.cycle_id, func.sum(CyclePoolCost.fixed_cost_cents)).group_by(CyclePoolCost.cycle_id)
            )}
            return {
                "cycles": [{"name": cycle.name, "start": self._format_timestamp(cycle.start_at_ms),
                            "end": self._format_timestamp(cycle.end_at_ms), "status": cycle.status,
                            "waiver": cycle.data_quality_waiver, "fixed_cost": format_cents(costs.get(cycle.id, 0))}
                           for cycle in cycles],
                "sync": [{"source": source.name, "last_event_id": checkpoint.last_event_id,
                          "last_event_at": self._format_timestamp(checkpoint.last_event_at_ms),
                          "last_success_at": self._format_timestamp(checkpoint.last_success_at_ms),
                          "backlog": checkpoint.backlog, "last_error": checkpoint.last_error}
                         for source, checkpoint in session.execute(
                             select(CPAMPSource, SyncCheckpoint).join(SyncCheckpoint, SyncCheckpoint.source_id == CPAMPSource.id)
                         )],
                "users": [{"id": user.telegram_user_id, "name": self._user_name(user, user.telegram_user_id),
                           "registered": bool(user.registered_at_ms), "manual_allowed": user.manual_allowed,
                           "active_keys": sum(1 for key in keys if key.current_owner_id == user.telegram_user_id and key.status == "active")}
                          for user in users.values()],
                "keys": [{"id": key.id, "masked": key.masked_value, "name": key.display_name,
                          "status": key.status, "owner_id": key.current_owner_id,
                          "owner": self._user_name(users.get(key.current_owner_id), key.current_owner_id or "未绑定"),
                          "created_at": self._format_timestamp(key.created_at_ms),
                          "revoked_at": self._format_timestamp(key.revoked_at_ms)} for key in keys],
                "ownership": [{"key_id": row.api_key_id,
                               "key": key_map[row.api_key_id].masked_value if row.api_key_id in key_map else str(row.api_key_id),
                               "user_id": row.telegram_user_id,
                               "user": self._user_name(users.get(row.telegram_user_id), row.telegram_user_id),
                               "from": self._format_timestamp(row.valid_from_ms), "to": self._format_timestamp(row.valid_to_ms),
                               "source": row.source, "reason": row.reason}
                              for row in session.scalars(select(KeyOwnershipPeriod).order_by(KeyOwnershipPeriod.valid_from_ms.desc()).limit(100))],
                "pools": [{"id": p.id, "name": p.name, "active": p.active} for p in session.scalars(select(ResourcePool).order_by(ResourcePool.id))],
                "pricing": [{"id": p.id, "name": p.name, "status": p.status, "source": p.source} for p in session.scalars(select(PricingVersion).order_by(PricingVersion.id.desc()))],
                "adjustments": [{"cycle": next((cycle.name for cycle in cycles if cycle.id == row.cycle_id), str(row.cycle_id)),
                                 "user_id": row.telegram_user_id, "amount": format_cents(row.amount_cents),
                                 "reason": row.reason, "operator": row.operator_user_id,
                                 "at": self._format_timestamp(row.created_at_ms)}
                                for row in session.scalars(select(Adjustment).order_by(Adjustment.id.desc()).limit(100))],
                "dead_letters": [{"id": d.id, "source_event_id": d.source_event_id, "error": d.error,
                                  "at": self._format_timestamp(d.created_at_ms)}
                                 for d in session.scalars(select(DeadLetter).where(DeadLetter.resolved_at_ms.is_(None)).order_by(DeadLetter.id.desc()).limit(50))],
                "audits": [{"operation": a.operation, "target": a.target,
                            "operator": f"{a.operator_type}:{a.operator_id}", "reason": a.reason,
                            "at": self._format_timestamp(a.created_at_ms)}
                           for a in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(50))],
            }

    def usage_summary(self) -> dict[str, Any]:
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            join_condition = and_(RatedEvent.raw_event_id == RawUsageEvent.id, RatedEvent.pricing_version_id == version_id)
            total = session.execute(select(func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd)).select_from(RawUsageEvent).outerjoin(RatedEvent, join_condition)).one()
            cutoff = now_ms() - 86_400_000
            recent = session.execute(select(func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd)).select_from(RawUsageEvent).outerjoin(RatedEvent, join_condition).where(RawUsageEvent.occurred_at_ms >= cutoff)).one()
            return {"total_requests": int(total[0] or 0), "total_tokens": int(total[1] or 0), "total_cost": format_usd_nano(int(total[2] or 0)),
                    "recent_requests": int(recent[0] or 0), "recent_tokens": int(recent[1] or 0), "recent_cost": format_usd_nano(int(recent[2] or 0))}

    def rankings(self, since_ms: int | None = None, until_ms: int | None = None,
                 pricing_version_id: int | None = None) -> list[dict[str, Any]]:
        with self.db.session() as session:
            version_id = pricing_version_id or self._active_pricing_id(session)
            ranking_source = (
                RawUsageEvent.__table__
                .outerjoin(APIKey.__table__, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
                .outerjoin(KeyOwnershipPeriod.__table__, and_(
                    KeyOwnershipPeriod.api_key_id == APIKey.id,
                    KeyOwnershipPeriod.valid_from_ms <= RawUsageEvent.occurred_at_ms,
                    or_(
                        KeyOwnershipPeriod.valid_to_ms.is_(None),
                        KeyOwnershipPeriod.valid_to_ms > RawUsageEvent.occurred_at_ms,
                    ),
                ))
                .outerjoin(RatedEvent.__table__, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ))
            )
            query = select(
                KeyOwnershipPeriod.telegram_user_id,
                func.count(RawUsageEvent.id),
                func.sum(RawUsageEvent.total_tokens),
                func.sum(RatedEvent.rated_weight_nano_usd),
            ).select_from(ranking_source)
            if since_ms is not None:
                query = query.where(RawUsageEvent.occurred_at_ms >= since_ms)
            if until_ms is not None:
                query = query.where(RawUsageEvent.occurred_at_ms < until_ms)
            rows = session.execute(
                query.group_by(KeyOwnershipPeriod.telegram_user_id)
                .order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc())
            ).all()
            usage = {user_id: (int(requests or 0), int(tokens or 0), int(cost or 0))
                     for user_id, requests, tokens, cost in rows}
            users = list(session.scalars(select(TelegramUser).where(TelegramUser.registered_at_ms.is_not(None))))
            result = []
            for user in users:
                requests, tokens, cost = usage.get(user.telegram_user_id, (0, 0, 0))
                key_count = session.scalar(select(func.count()).select_from(APIKey).where(
                    APIKey.current_owner_id == user.telegram_user_id, APIKey.status == "active")) or 0
                result.append({"telegram_user_id": user.telegram_user_id,
                               "name": self._user_name(user, user.telegram_user_id), "requests": requests,
                               "tokens": tokens, "cost": format_usd_nano(cost), "cost_nano": cost,
                               "key_count": int(key_count)})
            if None in usage:
                filters = [KeyOwnershipPeriod.telegram_user_id.is_(None)]
                if since_ms is not None:
                    filters.append(RawUsageEvent.occurred_at_ms >= since_ms)
                if until_ms is not None:
                    filters.append(RawUsageEvent.occurred_at_ms < until_ms)
                key_count = session.scalar(
                    select(func.count(func.distinct(RawUsageEvent.api_key_hash)))
                    .select_from(ranking_source)
                    .where(*filters)
                ) or 0
                requests, tokens, cost = usage[None]
                result.append({"telegram_user_id": None, "name": "未绑定 Telegram 的 API Key",
                               "requests": requests, "tokens": tokens, "cost": format_usd_nano(cost),
                               "cost_nano": cost, "key_count": int(key_count)})
            result.sort(key=lambda item: (item["cost_nano"], item["requests"]), reverse=True)
            for item in result:
                item.pop("cost_nano")
            return result

    def hourly_usage(self, hours: int = 24) -> tuple[list[str], list[dict[str, Any]]]:
        start = now_ms() - hours * 3_600_000
        with self.db.session() as session:
            ownership_source = (
                RawUsageEvent.__table__
                .outerjoin(APIKey.__table__, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
                .outerjoin(KeyOwnershipPeriod.__table__, and_(
                    KeyOwnershipPeriod.api_key_id == APIKey.id,
                    KeyOwnershipPeriod.valid_from_ms <= RawUsageEvent.occurred_at_ms,
                    or_(
                        KeyOwnershipPeriod.valid_to_ms.is_(None),
                        KeyOwnershipPeriod.valid_to_ms > RawUsageEvent.occurred_at_ms,
                    ),
                ))
            )
            rows = session.execute(
                select(KeyOwnershipPeriod.telegram_user_id, RawUsageEvent.occurred_at_ms, RawUsageEvent.total_tokens)
                .select_from(ownership_source)
                .where(RawUsageEvent.occurred_at_ms >= start)
            ).all()
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
            version_id = self._active_pricing_id(session)
            rows = session.execute(select(RawUsageEvent.model, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens),
                                          func.sum(RatedEvent.rated_weight_nano_usd)).outerjoin(
                                              RatedEvent, and_(RatedEvent.raw_event_id == RawUsageEvent.id,
                                                               RatedEvent.pricing_version_id == version_id))
                                   .group_by(RawUsageEvent.model).order_by(func.sum(RatedEvent.rated_weight_nano_usd).desc()).limit(limit)).all()
            return [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0), "cost": format_usd_nano(int(cost or 0))}
                    for model, requests, tokens, cost in rows]

    def account_usage(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            label = func.coalesce(func.nullif(RawUsageEvent.account_snapshot, ""), func.nullif(RawUsageEvent.source_label, ""), "-")
            rows = session.execute(select(label, func.count(RawUsageEvent.id), func.sum(RawUsageEvent.total_tokens), func.sum(RatedEvent.rated_weight_nano_usd))
                                   .outerjoin(RatedEvent, and_(RatedEvent.raw_event_id == RawUsageEvent.id,
                                                              RatedEvent.pricing_version_id == version_id)).group_by(label)
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
                        parts.append(f"reset={self._format_timestamp(latest.quota_recover_at_ms)}")
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

    def reconciliation(self, cycle_name: str | None = None, record: bool = False) -> dict[str, Any]:
        with self._cpamp() as cpamp:
            source_count = int(cpamp.execute("select count(*) from usage_events").fetchone()[0])
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
            raw_count = session.scalar(select(func.count()).select_from(RawUsageEvent)) or 0
            rated_count = session.scalar(select(func.count()).select_from(RatedEvent).where(RatedEvent.pricing_version_id == version_id)) or 0
            dead = session.scalar(select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved_at_ms.is_(None))) or 0
            unowned = session.scalar(select(func.count()).select_from(RatedEvent).where(
                RatedEvent.pricing_version_id == version_id, RatedEvent.telegram_user_id.is_(None))) or 0
            unassigned = session.scalar(select(func.count()).select_from(RatedEvent).where(
                RatedEvent.pricing_version_id == version_id, RatedEvent.pool_id.is_(None))) or 0
            result = {"cpamp_events": source_count, "raw_events": raw_count, "rated_events": rated_count, "dead_letters": dead,
                      "unowned_events": unowned, "unassigned_events": unassigned,
                      "ok": source_count == raw_count == rated_count and dead == 0 and unassigned == 0}
            if record:
                session.add(ReconciliationRun(cycle_id=None, result_json=json.dumps(result), ok=bool(result["ok"]), created_at_ms=now_ms()))
            return result

    def create_cycle(self, name: str, start: str, end: str, fixed_cost_cents: int, waiver: str | None = None,
                     operator_type: str | None = None, operator_id: str | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
            raise BillingError("cycle name must use letters, numbers, dot, underscore, or hyphen")
        if fixed_cost_cents < 0:
            raise BillingError("fixed cost cannot be negative")
        zone = ZoneInfo(self.settings.timezone)
        try:
            start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as exc:
            raise BillingError("cycle time is invalid") from exc
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=zone)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=zone)
        start_ms, end_ms = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)
        if end_ms <= start_ms:
            raise BillingError("cycle end must be after start")
        with self.db.session() as session:
            if session.scalar(select(BillingCycle).where(BillingCycle.name == name)):
                raise BillingError("billing cycle already exists")
            version_id = self._active_pricing_id(session)
            cycle = BillingCycle(name=name, start_at_ms=start_ms, end_at_ms=end_ms, timezone=self.settings.timezone, status="open",
                                 pricing_version_id=version_id, tiers_json=json.dumps(DEFAULT_TIERS), data_quality_waiver=waiver, created_at_ms=now_ms())
            session.add(cycle)
            session.flush()
            pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
            if pool is None:
                raise BillingError("default pool is missing")
            session.add(CyclePoolCost(cycle_id=cycle.id, pool_id=pool.id, fixed_cost_cents=fixed_cost_cents))
            if operator_type and operator_id:
                session.add(AuditLog(operator_type=operator_type, operator_id=operator_id, operation="cycle.create", target=name,
                                     after_json=json.dumps({"start_at_ms": start_ms, "end_at_ms": end_ms,
                                                            "fixed_cost_cents": fixed_cost_cents,
                                                            "waiver": waiver}), created_at_ms=now_ms()))
