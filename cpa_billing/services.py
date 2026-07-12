from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Integer, and_, case, cast, delete, func, or_, select, update
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
    GradientRule,
    GroupMembership,
    KeyActionRequest,
    KeyOwnershipPeriod,
    ManualUsageAdjustment,
    ModelPriceRule,
    MeteredKeyCharge,
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


class CPAMPClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.cpamp_base_url
        self.key = settings.cpamp_admin_key

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.key:
            raise BillingDependencyError("CPAMP 管理密钥未配置")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.key}"
        with httpx.Client(timeout=httpx.Timeout(45, connect=5)) as client:
            response = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def sync_model_prices(self, models: list[str]) -> dict[str, Any]:
        result = self._request("POST", "/v0/management/model-prices/sync", json={"models": models})
        if not isinstance(result, dict):
            raise BillingError("CPAMP model price sync response is invalid")
        return result


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
        with self.session() as client:
            try:
                update_status = self.request(client, "GET", "/update/check")
            except httpx.HTTPError:
                update_status = {"available": False, "error": "Keeper 更新检查不可用"}
            return {
                "status": self.request(client, "GET", "/status"),
                "version": self.request(client, "GET", "/version"),
                "update": update_status,
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


def _model_slug(value: str) -> str:
    return value.strip().lower().rsplit("/", 1)[-1]


def _model_family(value: str, family: str) -> bool:
    slug = _model_slug(value)
    return slug == family or slug.startswith(family + "-")


def _priority_multiplier_ppm(model: str) -> int:
    for family, multiplier in (
        ("gpt-5.6", 2_000_000),
        ("gpt-5.5", 2_500_000),
        ("gpt-5.4-mini", 2_000_000),
        ("gpt-5.4", 2_000_000),
        ("gpt-5.3-codex", 2_000_000),
    ):
        if _model_family(model, family):
            return multiplier
    return 1_000_000


def _official_gpt56_rates(model: str) -> tuple[int, int, int, int] | None:
    for family, prices in (
        ("gpt-5.6-sol", (5, 30, 0.5, 6.25)),
        ("gpt-5.6-terra", (2.5, 15, 0.25, 3.125)),
        ("gpt-5.6-luna", (1, 6, 0.1, 1.25)),
    ):
        if _model_family(model, family):
            return tuple(_nano_per_token(value) for value in prices)
    return None


def _scaled_rate(rate: int, multiplier_ppm: int) -> int:
    return (rate * multiplier_ppm + 500_000) // 1_000_000


def _metered_amount_cents(actual_nano_usd: int, multiplier_ppm: int) -> int:
    denominator = NANO_USD * 1_000_000
    return (actual_nano_usd * multiplier_ppm * 100 + denominator // 2) // denominator


@dataclass(frozen=True)
class CycleEstimate:
    user_lines: dict[int, list[tuple[int, int, int, int]]]
    metered_keys: list[dict[str, Any]]
    pool_totals: list[dict[str, Any]]
    adjustments: dict[int, int]
    generated_at_ms: int


class BillingService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database
        self.cpa = CPAClient(settings)
        self.cpamp = CPAMPClient(settings)
        self.keeper = KeeperClient(settings)
        self._quota_window_starts: dict[tuple[str, str], int] = {}
        self._quota_window_lock = threading.Lock()
        self._manual_usage_lock = threading.Lock()

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
            gradient = session.scalar(select(GradientRule).where(GradientRule.name == "default-gradient"))
            if gradient is None:
                gradient = GradientRule(
                    name="default-gradient",
                    description="Default progressive allocation rule",
                    tiers_json=json.dumps(DEFAULT_TIERS, separators=(",", ":")),
                    active=True,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                session.add(gradient)
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

    def _backfill_cpamp_reasoning_effort(
        self,
        source_db: sqlite3.Connection,
        session: Any,
        source_id: int,
        batch_size: int,
        has_reasoning_effort: bool,
    ) -> int:
        if not has_reasoning_effort:
            return 0
        pending = session.scalars(
            select(RawUsageEvent)
            .where(
                RawUsageEvent.source_id == source_id,
                RawUsageEvent.reasoning_effort.is_(None),
            )
            .order_by(RawUsageEvent.source_event_id)
            .limit(min(batch_size, 5000))
        ).all()
        if not pending:
            return 0
        source_rows = source_db.execute(
            "select id, reasoning_effort from usage_events where id between ? and ?",
            (pending[0].source_event_id, pending[-1].source_event_id),
        ).fetchall()
        effort_by_source_id = {
            int(row["id"]): str(row["reasoning_effort"] or "").strip()
            for row in source_rows
        }
        for event in pending:
            # Empty string means the source was checked and had no effort value.
            event.reasoning_effort = effort_by_source_id.get(event.source_event_id, "")
        return len(pending)

    def sync_cpamp(self, batch_size: int = 1000) -> int:
        imported = 0
        with self._cpamp() as source_db:
            fingerprint = self._schema_fingerprint(source_db)
            usage_columns = {str(item[1]) for item in source_db.execute("pragma table_info(usage_events)")}
            reasoning_effort_column = (
                "reasoning_effort" if "reasoning_effort" in usage_columns else "NULL AS reasoning_effort"
            )
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
                    f"""
                    select id,event_hash,request_id,timestamp_ms,timestamp,provider,executor_type,model,
                           requested_model,resolved_model,service_tier,api_key_hash,source_hash,source,
                           account_snapshot,auth_index,input_tokens,output_tokens,reasoning_tokens,
                           cached_tokens,cache_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,failed,
                           fail_status_code,latency_ms,ttft_ms,response_metadata_json,header_quota_used_percent,
                           header_quota_recover_at_ms,header_quota_plan_type,{reasoning_effort_column}
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
                            reasoning_effort=str(row["reasoning_effort"] or "").strip(),
                            service_tier=row["service_tier"], api_key_hash=row["api_key_hash"], source_hash=row["source_hash"],
                            source_label=row["source"], account_snapshot=row["account_snapshot"], auth_index=row["auth_index"],
                            input_tokens=int(row["input_tokens"] or 0), output_tokens=int(row["output_tokens"] or 0),
                            reasoning_tokens=int(row["reasoning_tokens"] or 0), cached_tokens=int(row["cached_tokens"] or 0),
                            cache_tokens=int(row["cache_tokens"] or 0),
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
                backfilled = self._backfill_cpamp_reasoning_effort(
                    source_db, session, source.id, max(batch_size, 5000), "reasoning_effort" in usage_columns,
                )
                if backfilled:
                    LOGGER.info("worker backfilled reasoning_effort=%s", backfilled)
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
        created = now_ms()
        with self.db.session() as session:
            for active in session.scalars(select(PricingVersion).where(PricingVersion.status == "active")):
                active.status = "retired"
            version = PricingVersion(
                name=name,
                status="active",
                source="CPAMP model_prices snapshot",
                created_at_ms=created,
                activated_at_ms=created,
            )
            session.add(version)
            session.flush()
            for row in rows:
                raw_text = str(row["raw_json"] or "")
                keys = set(row.keys())
                model = str(row["model"])
                input_rate = _nano_per_token(row["prompt_per_1m"])
                output_rate = _nano_per_token(row["completion_per_1m"])
                cache_read_rate = _nano_per_token(row["cache_read_per_1m"] or row["cache_per_1m"])
                cache_creation_rate = _nano_per_token(row["cache_creation_per_1m"])
                input_configured = bool(row["prompt_configured"]) if "prompt_configured" in keys else True
                output_configured = bool(row["completion_configured"]) if "completion_configured" in keys else True
                cache_read_configured = bool(row["cache_read_configured"]) if "cache_read_configured" in keys else cache_read_rate > 0
                cache_creation_configured = bool(row["cache_creation_configured"]) if "cache_creation_configured" in keys else cache_creation_rate > 0
                if _model_family(model, "gpt-5.6"):
                    official = _official_gpt56_rates(model)
                    if official is not None:
                        if not input_configured and input_rate == 0:
                            input_rate = official[0]
                        if not output_configured and output_rate == 0:
                            output_rate = official[1]
                    if not cache_read_configured:
                        cache_read_rate = _scaled_rate(input_rate, 100_000)
                    if not cache_creation_configured:
                        cache_creation_rate = _scaled_rate(input_rate, 1_250_000)
                priority_multiplier = _priority_multiplier_ppm(model)
                session.add(ModelPriceRule(
                    pricing_version_id=version.id,
                    model=model,
                    input_nano_per_token=input_rate,
                    output_nano_per_token=output_rate,
                    cache_read_nano_per_token=cache_read_rate,
                    cache_creation_nano_per_token=cache_creation_rate,
                    input_configured=input_configured,
                    output_configured=output_configured,
                    cache_read_configured=cache_read_configured,
                    cache_creation_configured=cache_creation_configured,
                    priority_input_nano_per_token=_scaled_rate(input_rate, priority_multiplier) if priority_multiplier != 1_000_000 else None,
                    priority_output_nano_per_token=_scaled_rate(output_rate, priority_multiplier) if priority_multiplier != 1_000_000 else None,
                    priority_cache_read_nano_per_token=_scaled_rate(cache_read_rate, priority_multiplier) if priority_multiplier != 1_000_000 else None,
                    priority_cache_creation_nano_per_token=_scaled_rate(cache_creation_rate, priority_multiplier) if priority_multiplier != 1_000_000 else None,
                    flex_input_nano_per_token=None,
                    flex_output_nano_per_token=None,
                    long_threshold_tokens=272_000 if _model_family(model, "gpt-5.6") else None,
                    long_input_multiplier_ppm=2_000_000 if _model_family(model, "gpt-5.6") else 1_000_000,
                    long_output_multiplier_ppm=1_500_000 if _model_family(model, "gpt-5.6") else 1_000_000,
                    raw_json=raw_text or None,
                ))
            if operator_type and operator_id:
                session.add(AuditLog(operator_type=operator_type, operator_id=operator_id, operation="pricing.import",
                                     target=name, after_json=json.dumps({"models": len(rows)}), created_at_ms=now_ms()))
            return version.id

    def _invalidate_cycle_previews(self, session: Any, cycle_ids: list[int]) -> None:
        if not cycle_ids:
            return
        statement_ids = select(Statement.id).where(Statement.cycle_id.in_(cycle_ids))
        session.execute(delete(StatementLine).where(StatementLine.statement_id.in_(statement_ids)))
        session.execute(delete(Statement).where(Statement.cycle_id.in_(cycle_ids)))
        session.execute(delete(MeteredKeyCharge).where(MeteredKeyCharge.cycle_id.in_(cycle_ids)))
        session.execute(
            update(BillingCycle)
            .where(BillingCycle.id.in_(cycle_ids), BillingCycle.status == "preview")
            .values(status="open")
        )

    def sync_upstream_prices(
        self,
        name: str | None,
        operator_type: str,
        operator_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise BillingError("价格同步必须填写原因")
        with self.db.session() as session:
            used_models = sorted({
                str(value).strip()
                for row in session.execute(select(
                    RawUsageEvent.resolved_model,
                    RawUsageEvent.requested_model,
                    RawUsageEvent.model,
                ))
                for value in row
                if value and str(value).strip()
            })
        try:
            upstream = self.cpamp.sync_model_prices(used_models)
        except httpx.HTTPError as exc:
            raise BillingDependencyError("CPAMP 上游价格同步失败") from exc
        version_name = (name or "").strip() or f"cpamp-{datetime.now(ZoneInfo(self.settings.timezone)):%Y%m%d-%H%M%S}"
        version_id = self.import_cpamp_prices(
            version_name,
            operator_type=operator_type,
            operator_id=operator_id,
            allow_existing=False,
        )
        with self.db.session() as session:
            cycles = list(session.scalars(select(BillingCycle).where(BillingCycle.status != "closed")))
            cycle_ids = [cycle.id for cycle in cycles]
            for cycle in cycles:
                cycle.pricing_version_id = version_id
            self._invalidate_cycle_previews(session, cycle_ids)
            session.add(AuditLog(
                operator_type=operator_type,
                operator_id=operator_id,
                operation="pricing.sync",
                target=version_name,
                after_json=json.dumps({
                    "version_id": version_id,
                    "cycles": [cycle.name for cycle in cycles],
                    "source": upstream.get("source"),
                    "imported": upstream.get("imported"),
                    "unmatched": upstream.get("unmatched") or [],
                }),
                reason=reason.strip(),
                created_at_ms=now_ms(),
            ))
            bounds = (
                min((cycle.start_at_ms for cycle in cycles), default=None),
                max((cycle.end_at_ms for cycle in cycles), default=None),
            )
        rated = 0
        if bounds[0] is not None and bounds[1] is not None:
            while True:
                batch = self.rate_events(limit=5000, version_id=version_id, start_ms=bounds[0], end_ms=bounds[1])
                rated += batch
                if batch == 0:
                    break
        return {
            "version_id": version_id,
            "name": version_name,
            "source": upstream.get("source"),
            "sources": upstream.get("sources") or [],
            "imported": int(upstream.get("imported") or 0),
            "skipped": int(upstream.get("skipped") or 0),
            "unmatched": upstream.get("unmatched") or [],
            "rated_events": rated,
        }

    def _active_pricing_id(self, session: Any) -> int:
        version = session.scalar(select(PricingVersion).where(PricingVersion.status == "active").order_by(PricingVersion.id.desc()))
        if version is None:
            raise BillingError("no active pricing version")
        return version.id

    def _rate_until_current(self, version_id: int, start_ms: int | None = None,
                            end_ms: int | None = None) -> int:
        total = 0
        while True:
            rated = self.rate_events(
                limit=5000,
                version_id=version_id,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            total += rated
            if rated == 0:
                return total

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

    @staticmethod
    def _compatible_cached_tokens(event: RawUsageEvent) -> int:
        cached = max(int(event.cached_tokens or 0), int(event.cache_tokens or 0))
        fine_grained = max(int(event.cache_read_tokens or 0), 0) + max(int(event.cache_creation_tokens or 0), 0)
        return max(cached - fine_grained, 0)

    @classmethod
    def _effective_cache_read_tokens(cls, event: RawUsageEvent) -> int:
        return cls._compatible_cached_tokens(event) + max(int(event.cache_read_tokens or 0), 0)

    def _rate_event(self, behavior_model: str, price_model: str, rule: ModelPriceRule,
                    event: RawUsageEvent, tier: str) -> tuple[int, bool, dict[str, Any]]:
        input_rate = rule.input_nano_per_token
        output_rate = rule.output_nano_per_token
        cache_read_rate = rule.cache_read_nano_per_token
        cache_creation_rate = rule.cache_creation_nano_per_token
        if tier == "priority":
            input_rate = rule.priority_input_nano_per_token or input_rate
            output_rate = rule.priority_output_nano_per_token or output_rate
            cache_read_rate = rule.priority_cache_read_nano_per_token or cache_read_rate
            cache_creation_rate = rule.priority_cache_creation_nano_per_token or cache_creation_rate

        long_context = bool(
            _model_family(behavior_model, "gpt-5.6")
            and event.input_tokens > 272_000
        )
        if long_context:
            input_rate = _scaled_rate(input_rate, 2_000_000)
            cache_read_rate = _scaled_rate(cache_read_rate, 2_000_000)
            cache_creation_rate = _scaled_rate(cache_creation_rate, 2_000_000)
            output_rate = _scaled_rate(output_rate, 1_500_000)

        compatible_cached = self._compatible_cached_tokens(event)
        cache_read = max(int(event.cache_read_tokens or 0), 0)
        cache_creation = max(int(event.cache_creation_tokens or 0), 0)
        input_tokens = max(int(event.input_tokens or 0), 0)
        output_tokens = max(int(event.output_tokens or 0), 0)
        if _model_family(behavior_model, "gpt-5.6"):
            read_tokens = compatible_cached + cache_read
            uncached = max(input_tokens - read_tokens - cache_creation, 0)
            cost = (
                uncached * input_rate
                + read_tokens * cache_read_rate
                + cache_creation * cache_creation_rate
                + output_tokens * output_rate
            )
        elif cache_read or cache_creation:
            uncached = max(input_tokens - compatible_cached, 0)
            effective_creation_rate = cache_creation_rate if rule.cache_creation_configured or cache_creation_rate > 0 else input_rate
            cost = (
                uncached * input_rate
                + compatible_cached * cache_read_rate
                + cache_read * cache_read_rate
                + cache_creation * effective_creation_rate
                + output_tokens * output_rate
            )
            cache_creation_rate = effective_creation_rate
        else:
            uncached = max(input_tokens - compatible_cached, 0)
            cost = uncached * input_rate + compatible_cached * cache_read_rate + output_tokens * output_rate
        detail = {
            "behavior_model": behavior_model,
            "price_model": price_model,
            "tier": tier,
            "uncached": uncached,
            "cached": compatible_cached,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "output": output_tokens,
            "rates": [input_rate, cache_read_rate, cache_creation_rate, output_rate],
        }
        return cost, long_context, detail

    def rate_events(self, limit: int = 2000, version_id: int | None = None,
                    start_ms: int | None = None, end_ms: int | None = None) -> int:
        rated = 0
        with self.db.session() as session:
            selected_version_id = version_id or self._active_pricing_id(session)
            prices = {
                rule.model: rule
                for rule in session.scalars(
                    select(ModelPriceRule).where(ModelPriceRule.pricing_version_id == selected_version_id)
                )
            }
            if not prices:
                return 0
            price_models = list(prices)
            filters = [
                RatedEvent.id.is_(None),
                or_(
                    RawUsageEvent.resolved_model.in_(price_models),
                    RawUsageEvent.requested_model.in_(price_models),
                    RawUsageEvent.model.in_(price_models),
                ),
            ]
            if start_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms >= start_ms)
            if end_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms < end_ms)
            events = session.scalars(
                select(RawUsageEvent).outerjoin(
                    RatedEvent,
                    and_(RatedEvent.raw_event_id == RawUsageEvent.id, RatedEvent.pricing_version_id == selected_version_id),
                ).where(*filters).order_by(RawUsageEvent.id).limit(limit)
            ).all()
            for event in events:
                candidates = []
                for candidate in (event.resolved_model, event.requested_model, event.model):
                    if candidate and candidate not in candidates:
                        candidates.append(candidate)
                behavior_model = candidates[0] if candidates else event.model
                price_model = next((candidate for candidate in candidates if candidate in prices), None)
                rule = prices.get(price_model) if price_model else None
                if rule is None:
                    continue
                tier = (event.service_tier or "default").lower()
                if tier == "fast":
                    tier = "priority"
                cost, long_context, detail = self._rate_event(behavior_model, price_model, rule, event, tier)
                rated_event = RatedEvent(
                    raw_event_id=event.id, pricing_version_id=selected_version_id, pool_id=self._pool_for(session, event),
                    telegram_user_id=self._owner_at(session, event.api_key_hash, event.occurred_at_ms),
                    occurred_at_ms=event.occurred_at_ms, rated_weight_nano_usd=cost,
                    long_context_applied=long_context, service_tier=tier, calculation_json=json.dumps(detail, separators=(",", ":")), rated_at_ms=now_ms(),
                )
                try:
                    with session.begin_nested():
                        session.add(rated_event)
                        session.flush()
                except IntegrityError:
                    continue
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
        created = now_ms()
        row = APIKey(
            cpamp_hash=key_hash,
            login_fingerprint=login_fingerprint(raw_key, self.settings.key_pepper),
            masked_value=mask_api_key(raw_key),
            status="active",
            current_owner_id=owner_id,
            present_in_cpa=True,
            last_seen_in_cpa_at_ms=created,
            created_at_ms=created,
        )
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

    def sync_cpa_keys(self) -> dict[str, int]:
        raw_keys = self.cpa.list_keys()
        observed = now_ms()
        current_hashes = {cpamp_key_hash(raw): raw for raw in raw_keys}
        counts = {"created": 0, "updated": 0, "retired": 0, "current": len(raw_keys)}
        with self.db.session() as session:
            existing = {key.cpamp_hash: key for key in session.scalars(select(APIKey))}
            for key_hash, raw in current_hashes.items():
                fingerprint = login_fingerprint(raw, self.settings.key_pepper)
                key = existing.get(key_hash)
                if key is None:
                    session.add(APIKey(
                        cpamp_hash=key_hash,
                        login_fingerprint=fingerprint,
                        masked_value=mask_api_key(raw),
                        status="unowned",
                        current_owner_id=None,
                        present_in_cpa=True,
                        last_seen_in_cpa_at_ms=observed,
                        created_at_ms=observed,
                    ))
                    counts["created"] += 1
                    continue
                key.masked_value = mask_api_key(raw)
                if key.login_fingerprint is None:
                    key.login_fingerprint = fingerprint
                key.present_in_cpa = True
                key.last_seen_in_cpa_at_ms = observed
                if key.status == "retired":
                    key.status = "active" if key.current_owner_id is not None else "unowned"
                counts["updated"] += 1
            for key_hash, key in existing.items():
                if key_hash in current_hashes:
                    continue
                key.present_in_cpa = False
                if key.status in {"active", "unowned"}:
                    key.status = "retired"
                    counts["retired"] += 1
        return counts

    @staticmethod
    def _multiplier_ppm(value: str | None) -> int | None:
        if value is None or not value.strip():
            return None
        try:
            multiplier = Decimal(value.strip())
        except InvalidOperation as exc:
            raise BillingError("按量倍率格式无效") from exc
        if not multiplier.is_finite() or multiplier < 0 or multiplier > 1000:
            raise BillingError("按量倍率必须在 0 到 1000 之间")
        return int((multiplier * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))

    def update_unowned_key_profile(self, key_id: int, name: str | None, multiplier: str | None,
                                   reason: str | None, operator_id: str = "admin-token") -> dict[str, Any]:
        normalized_reason = (reason or "").strip() or None
        multiplier_ppm = self._multiplier_ppm(multiplier)
        with self.db.session() as session:
            key = session.get(APIKey, key_id)
            if key is None or key.current_owner_id is not None:
                raise BillingError("只能配置未绑定 Telegram 用户的 API Key")
            before = {"name": key.display_name, "multiplier_ppm": key.billing_multiplier_ppm}
            key.display_name = (name or "").strip()[:120] or None
            key.billing_multiplier_ppm = multiplier_ppm
            cycle_ids = list(session.scalars(select(BillingCycle.id).where(BillingCycle.status != "closed")))
            self._invalidate_cycle_previews(session, cycle_ids)
            session.add(AuditLog(
                operator_type="web-admin",
                operator_id=operator_id,
                operation="key.billing-profile.update",
                target=str(key.id),
                before_json=json.dumps(before),
                after_json=json.dumps({"name": key.display_name, "multiplier_ppm": multiplier_ppm}),
                reason=normalized_reason,
                created_at_ms=now_ms(),
            ))
            return {
                "id": key.id,
                "masked": key.masked_value,
                "name": key.display_name,
                "multiplier": None if multiplier_ppm is None else format(Decimal(multiplier_ppm) / Decimal(1_000_000), "f"),
            }

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

    def set_user_admin(
        self,
        user_id: int,
        is_admin: bool,
        reason: str,
        operator_id: int | None = None,
        operator_type: str = "web-admin",
    ) -> dict[str, Any]:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BillingError("管理员权限变更原因不能为空")
        if user_id in self.settings.admin_user_ids and not is_admin:
            raise BillingError("配置文件中的管理员不能在面板撤销，请修改 ADMIN_CHAT_IDS")
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None:
                raise BillingError("Telegram 用户不存在")
            before = bool(user.is_admin)
            if before != is_admin:
                user.is_admin = is_admin
                session.add(AuditLog(
                    operator_type=operator_type,
                    operator_id=str(operator_id if operator_id is not None else "admin-token"),
                    operation="user.admin.grant" if is_admin else "user.admin.revoke",
                    target=str(user_id),
                    before_json=json.dumps({"is_admin": before}),
                    after_json=json.dumps({"is_admin": is_admin}),
                    reason=normalized_reason,
                    created_at_ms=now_ms(),
                ))
            effective = bool(user.is_admin or user_id in self.settings.admin_user_ids)
            return {
                "id": user_id,
                "is_admin": effective,
                "configured_admin": user_id in self.settings.admin_user_ids,
            }

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
                    target.present_in_cpa = False
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

    def _validate_cycle_rating(self, session: Any, cycle: BillingCycle) -> None:
        unpriced = session.scalar(select(func.count()).select_from(RawUsageEvent).outerjoin(
            RatedEvent, and_(
                RatedEvent.raw_event_id == RawUsageEvent.id,
                RatedEvent.pricing_version_id == cycle.pricing_version_id,
            )
        ).where(
            RawUsageEvent.occurred_at_ms >= cycle.start_at_ms,
            RawUsageEvent.occurred_at_ms < cycle.end_at_ms,
            RatedEvent.id.is_(None),
        )) or 0
        if unpriced:
            raise BillingError(f"cycle has {unpriced} unrated events")
        unassigned = session.scalar(select(func.count()).select_from(RatedEvent).where(
            RatedEvent.pricing_version_id == cycle.pricing_version_id,
            RatedEvent.occurred_at_ms >= cycle.start_at_ms,
            RatedEvent.occurred_at_ms < cycle.end_at_ms,
            RatedEvent.pool_id.is_(None),
        )) or 0
        if unassigned:
            raise BillingError(f"cycle has {unassigned} unassigned events")

    def _build_cycle_estimate(self, session: Any, cycle: BillingCycle, strict: bool) -> CycleEstimate:
        period = (
            RatedEvent.pricing_version_id == cycle.pricing_version_id,
            RatedEvent.occurred_at_ms >= cycle.start_at_ms,
            RatedEvent.occurred_at_ms < cycle.end_at_ms,
        )
        pool_users: dict[int, dict[int, int]] = defaultdict(dict)
        for pool_id, user_id, weight in session.execute(
            select(RatedEvent.pool_id, RatedEvent.telegram_user_id, func.sum(RatedEvent.rated_weight_nano_usd))
            .where(*period, RatedEvent.telegram_user_id.is_not(None), RatedEvent.pool_id.is_not(None))
            .group_by(RatedEvent.pool_id, RatedEvent.telegram_user_id)
        ):
            pool_users[int(pool_id)][int(user_id)] = int(weight or 0)
        for pool_id, user_id, weight in session.execute(
            select(
                ManualUsageAdjustment.pool_id,
                ManualUsageAdjustment.telegram_user_id,
                func.sum(ManualUsageAdjustment.amount_nano_usd),
            )
            .where(ManualUsageAdjustment.cycle_id == cycle.id)
            .group_by(ManualUsageAdjustment.pool_id, ManualUsageAdjustment.telegram_user_id)
        ):
            manual_weight = int(weight or 0)
            if manual_weight < 0:
                raise BillingError("手动原始用量余额不能为负数")
            pool = pool_users[int(pool_id)]
            pool[int(user_id)] = pool.get(int(user_id), 0) + manual_weight

        unowned_usage = session.execute(
            select(
                RatedEvent.pool_id,
                RawUsageEvent.api_key_hash,
                func.count(RatedEvent.id),
                func.sum(RawUsageEvent.total_tokens),
                func.sum(RatedEvent.rated_weight_nano_usd),
            )
            .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
            .where(*period, RatedEvent.telegram_user_id.is_(None), RatedEvent.pool_id.is_not(None))
            .group_by(RatedEvent.pool_id, RawUsageEvent.api_key_hash)
        ).all()
        hashes = {str(row[1]) for row in unowned_usage if row[1]}
        key_by_hash = {
            key.cpamp_hash: key
            for key in session.scalars(select(APIKey).where(APIKey.cpamp_hash.in_(hashes)))
        } if hashes else {}
        costs = {
            row.pool_id: int(row.fixed_cost_cents)
            for row in session.scalars(select(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id))
        }
        pools = {pool.id: pool.name for pool in session.scalars(select(ResourcePool))}
        tiers = parse_tiers(json.loads(cycle.tiers_json))
        metered_by_pool: dict[int, list[dict[str, Any]]] = defaultdict(list)
        metered_keys: list[dict[str, Any]] = []
        for pool_id, key_hash, requests, tokens, actual in unowned_usage:
            key = key_by_hash.get(str(key_hash or ""))
            if key is None or key.billing_multiplier_ppm is None:
                continue
            amount = _metered_amount_cents(int(actual or 0), int(key.billing_multiplier_ppm))
            item = {
                "pool_id": int(pool_id),
                "pool": pools.get(int(pool_id), str(pool_id)),
                "key_id": key.id,
                "masked": key.masked_value,
                "name": key.display_name,
                "requests": int(requests or 0),
                "tokens": int(tokens or 0),
                "actual_nano_usd": int(actual or 0),
                "multiplier_ppm": int(key.billing_multiplier_ppm),
                "amount_cents": amount,
            }
            metered_by_pool[int(pool_id)].append(item)
            metered_keys.append(item)

        user_lines: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        pool_totals: list[dict[str, Any]] = []
        for pool_id in sorted(costs.keys() | pool_users.keys() | metered_by_pool.keys()):
            fixed = int(costs.get(pool_id, 0))
            metered = sum(item["amount_cents"] for item in metered_by_pool.get(pool_id, []))
            residual = max(0, fixed - metered)
            users = pool_users.get(pool_id, {})
            billed = {uid: tiered_weight(weight, tiers) for uid, weight in users.items()}
            if strict and residual and not any(weight > 0 for weight in billed.values()):
                raise BillingError(f"pool {pool_id} has residual cost but no billable Telegram usage")
            allocated = largest_remainder(residual, billed)
            for user_id, actual in users.items():
                user_lines[user_id].append((pool_id, actual, billed[user_id], allocated[user_id]))
            member_amount = sum(allocated.values())
            pool_totals.append({
                "pool_id": pool_id,
                "pool": pools.get(pool_id, str(pool_id)),
                "fixed_cost_cents": fixed,
                "metered_amount_cents": metered,
                "residual_cost_cents": residual,
                "member_amount_cents": member_amount,
                "surplus_cents": max(0, metered - fixed),
                "unallocated_cents": max(0, residual - member_amount),
            })
        adjustments: dict[int, int] = defaultdict(int)
        for row in session.scalars(select(Adjustment).where(Adjustment.cycle_id == cycle.id)):
            adjustments[row.telegram_user_id] += int(row.amount_cents)
        return CycleEstimate(
            user_lines=dict(user_lines),
            metered_keys=sorted(metered_keys, key=lambda item: (item["amount_cents"], item["actual_nano_usd"]), reverse=True),
            pool_totals=pool_totals,
            adjustments=dict(adjustments),
            generated_at_ms=now_ms(),
        )

    def _persist_cycle_estimate(self, session: Any, cycle: BillingCycle, estimate: CycleEstimate) -> None:
        statement_ids = select(Statement.id).where(Statement.cycle_id == cycle.id)
        session.execute(delete(StatementLine).where(StatementLine.statement_id.in_(statement_ids)))
        session.execute(delete(Statement).where(Statement.cycle_id == cycle.id))
        session.execute(delete(MeteredKeyCharge).where(MeteredKeyCharge.cycle_id == cycle.id))
        for user_id in estimate.user_lines.keys() | estimate.adjustments.keys():
            user_lines = estimate.user_lines.get(user_id, [])
            statement = Statement(
                cycle_id=cycle.id,
                telegram_user_id=user_id,
                actual_weight_nano_usd=sum(value[1] for value in user_lines),
                billed_weight_nano_usd=sum(value[2] for value in user_lines),
                amount_cents=sum(value[3] for value in user_lines) + estimate.adjustments.get(user_id, 0),
                adjustment_cents=estimate.adjustments.get(user_id, 0),
                generated_at_ms=estimate.generated_at_ms,
                final=False,
            )
            session.add(statement)
            session.flush()
            key_count = session.scalar(
                select(func.count()).select_from(APIKey).where(APIKey.current_owner_id == user_id)
            ) or 0
            for pool_id, actual, billed, amount in user_lines:
                session.add(StatementLine(
                    statement_id=statement.id,
                    pool_id=pool_id,
                    actual_weight_nano_usd=actual,
                    billed_weight_nano_usd=billed,
                    amount_cents=amount,
                    api_key_count=int(key_count),
                ))
        for item in estimate.metered_keys:
            session.add(MeteredKeyCharge(
                cycle_id=cycle.id,
                pool_id=item["pool_id"],
                api_key_id=item["key_id"],
                actual_weight_nano_usd=item["actual_nano_usd"],
                multiplier_ppm=item["multiplier_ppm"],
                amount_cents=item["amount_cents"],
                generated_at_ms=estimate.generated_at_ms,
                final=False,
            ))

    def preview_cycle(self, cycle_name: str) -> list[Statement]:
        with self.db.session() as session:
            cycle = self._cycle(session, cycle_name)
            if cycle is None:
                raise BillingError("billing cycle not found")
            if cycle.status == "closed":
                return list(session.scalars(
                    select(Statement).where(Statement.cycle_id == cycle.id).order_by(Statement.amount_cents.desc())
                ))
            self._validate_cycle_rating(session, cycle)
            estimate = self._build_cycle_estimate(session, cycle, strict=True)
            self._persist_cycle_estimate(session, cycle, estimate)
            cycle.status = "preview"
            session.flush()
            return list(session.scalars(
                select(Statement).where(Statement.cycle_id == cycle.id).order_by(Statement.amount_cents.desc())
            ))

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
            session.execute(update(MeteredKeyCharge).where(MeteredKeyCharge.cycle_id == cycle.id).values(final=True))
            session.add(AuditLog(operator_type=operator_type, operator_id=str(operator_id if operator_id is not None else "admin-token"),
                                 operation="cycle.close", target=cycle.name,
                                 after_json=json.dumps({"waiver": cycle.data_quality_waiver}), created_at_ms=now_ms()))

    def dashboard(self, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            selected = self._display_cycle(session, cycle_name)
            selected_bounds = None if selected is None or selected.status == "closed" else (
                selected.pricing_version_id,
                selected.start_at_ms,
                selected.end_at_ms,
            )
        if selected_bounds:
            self._rate_until_current(*selected_bounds)
        with self.db.session() as session:
            cycle = self._display_cycle(session, cycle_name)
            cycles = list(session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc())))
            if cycle is None:
                return {"cycle": None, "cycles": [{"name": item.name, "status": item.status} for item in cycles],
                        "rows": [], "models": [], "metered_keys": [], "pool_totals": [],
                        "totals": {"requests": 0, "tokens": 0, "actual": "0.0000",
                                   "request_actual": "0.0000", "manual_actual": "0.0000", "billed": "0.0000",
                                   "member_amount": "0.00", "metered_amount": "0.00", "amount": "0.00"}}

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
            manual_usage = {
                int(user_id): int(weight or 0)
                for user_id, weight in session.execute(
                    select(
                        ManualUsageAdjustment.telegram_user_id,
                        func.sum(ManualUsageAdjustment.amount_nano_usd),
                    )
                    .where(ManualUsageAdjustment.cycle_id == cycle.id)
                    .group_by(ManualUsageAdjustment.telegram_user_id)
                )
            }
            if cycle.status == "closed":
                statements = {row.telegram_user_id: row for row in session.scalars(
                    select(Statement).where(Statement.cycle_id == cycle.id)
                )}
                key_map = {key.id: key for key in session.scalars(select(APIKey))}
                pool_map = {pool.id: pool.name for pool in session.scalars(select(ResourcePool))}
                metered_stats = {
                    (int(pool_id), int(key_id)): (int(requests or 0), int(tokens or 0))
                    for pool_id, key_id, requests, tokens in session.execute(
                        select(
                            RatedEvent.pool_id,
                            APIKey.id,
                            func.count(RatedEvent.id),
                            func.sum(RawUsageEvent.total_tokens),
                        )
                        .join(RawUsageEvent, RawUsageEvent.id == RatedEvent.raw_event_id)
                        .join(APIKey, APIKey.cpamp_hash == RawUsageEvent.api_key_hash)
                        .where(*period, RatedEvent.telegram_user_id.is_(None))
                        .group_by(RatedEvent.pool_id, APIKey.id)
                    )
                    if pool_id is not None
                }
                metered_keys = [{
                    "pool_id": item.pool_id,
                    "pool": pool_map.get(item.pool_id, str(item.pool_id)),
                    "key_id": item.api_key_id,
                    "masked": key_map[item.api_key_id].masked_value if item.api_key_id in key_map else "key:****",
                    "name": key_map[item.api_key_id].display_name if item.api_key_id in key_map else None,
                    "requests": metered_stats.get((item.pool_id, item.api_key_id), (0, 0))[0],
                    "tokens": metered_stats.get((item.pool_id, item.api_key_id), (0, 0))[1],
                    "actual_nano_usd": item.actual_weight_nano_usd,
                    "multiplier_ppm": item.multiplier_ppm,
                    "amount_cents": item.amount_cents,
                } for item in session.scalars(select(MeteredKeyCharge).where(MeteredKeyCharge.cycle_id == cycle.id))]
                live_user = {
                    user_id: (statement.actual_weight_nano_usd, statement.billed_weight_nano_usd, statement.amount_cents)
                    for user_id, statement in statements.items()
                }
                fixed_by_pool = {item.pool_id: item.fixed_cost_cents for item in session.scalars(
                    select(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id)
                )}
                pool_totals = []
                for pool_id, fixed in fixed_by_pool.items():
                    metered = sum(item["amount_cents"] for item in metered_keys if item["pool_id"] == pool_id)
                    member = sum(line.amount_cents for line in session.scalars(
                        select(StatementLine).join(Statement).where(
                            Statement.cycle_id == cycle.id,
                            StatementLine.pool_id == pool_id,
                        )
                    ))
                    pool_totals.append({
                        "pool_id": pool_id, "pool": pool_map.get(pool_id, str(pool_id)),
                        "fixed_cost_cents": int(fixed), "metered_amount_cents": metered,
                        "residual_cost_cents": max(0, int(fixed) - metered), "member_amount_cents": member,
                        "surplus_cents": max(0, metered - int(fixed)), "unallocated_cents": 0,
                    })
                generated_at_ms = max((row.generated_at_ms for row in statements.values()), default=cycle.closed_at_ms or now_ms())
            else:
                estimate = self._build_cycle_estimate(session, cycle, strict=False)
                live_user = {
                    user_id: (
                        sum(value[1] for value in lines),
                        sum(value[2] for value in lines),
                        sum(value[3] for value in lines) + estimate.adjustments.get(user_id, 0),
                    )
                    for user_id, lines in estimate.user_lines.items()
                }
                for user_id, adjustment in estimate.adjustments.items():
                    live_user.setdefault(user_id, (0, 0, adjustment))
                statements = {}
                metered_keys = estimate.metered_keys
                pool_totals = estimate.pool_totals
                generated_at_ms = estimate.generated_at_ms
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
                requests, tokens, request_actual = usage.get(user.telegram_user_id, (0, 0, 0))
                estimate_values = live_user.get(user.telegram_user_id, (0, 0, 0))
                manual_actual = manual_usage.get(user.telegram_user_id, 0)
                actual = request_actual + manual_actual
                rows.append({
                    "telegram_user_id": user.telegram_user_id,
                    "name": self._user_name(user, user.telegram_user_id),
                    "requests": requests,
                    "tokens": tokens,
                    "actual": format_usd_nano(actual),
                    "actual_nano": actual,
                    "request_actual": format_usd_nano(request_actual),
                    "request_actual_nano": request_actual,
                    "manual_actual": format_usd_nano(manual_actual),
                    "manual_actual_nano": manual_actual,
                    "billed": format_usd_nano(estimate_values[1]),
                    "amount": format_cents(estimate_values[2]),
                    "amount_cents": estimate_values[2],
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
                    "request_actual": format_usd_nano(actual),
                    "request_actual_nano": actual,
                    "manual_actual": "0.0000",
                    "manual_actual_nano": 0,
                    "billed": "0.0000",
                    "amount": format_cents(sum(item["amount_cents"] for item in metered_keys)),
                    "amount_cents": sum(item["amount_cents"] for item in metered_keys),
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
            member_amount_cents = sum(item["amount_cents"] for item in rows if not item["unowned"])
            metered_amount_cents = sum(item["amount_cents"] for item in metered_keys)
            unpriced_events = int(session.scalar(
                select(func.count()).select_from(RawUsageEvent)
                .outerjoin(RatedEvent, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == cycle.pricing_version_id,
                ))
                .where(
                    RawUsageEvent.occurred_at_ms >= cycle.start_at_ms,
                    RawUsageEvent.occurred_at_ms < cycle.end_at_ms,
                    RatedEvent.id.is_(None),
                )
            ) or 0)
            gradient = session.get(GradientRule, cycle.gradient_rule_id)
            version = session.get(PricingVersion, cycle.pricing_version_id)
            return {
                "cycle": {"name": cycle.name, "status": cycle.status, "start": self._format_timestamp(cycle.start_at_ms),
                          "end": self._format_timestamp(cycle.end_at_ms), "waiver": cycle.data_quality_waiver,
                          "gradient_rule_id": cycle.gradient_rule_id,
                          "gradient_rule": gradient.name if gradient else None,
                          "pricing_version_id": cycle.pricing_version_id,
                          "pricing_version": version.name if version else None,
                          "estimate_live": cycle.status != "closed",
                          "estimate_complete": unpriced_events == 0,
                          "unpriced_events": unpriced_events,
                          "estimate_generated_at": self._iso_timestamp(generated_at_ms)},
                "cycles": [{"name": item.name, "status": item.status} for item in cycles],
                "rows": rows,
                "metered_keys": [{
                    **item,
                    "actual": format_usd_nano(item["actual_nano_usd"]),
                    "multiplier": format(Decimal(item["multiplier_ppm"]) / Decimal(1_000_000), "f"),
                    "amount": format_cents(item["amount_cents"]),
                } for item in metered_keys],
                "pool_totals": [{
                    **item,
                    "fixed_cost": format_cents(item["fixed_cost_cents"]),
                    "metered_amount": format_cents(item["metered_amount_cents"]),
                    "residual_cost": format_cents(item["residual_cost_cents"]),
                    "member_amount": format_cents(item["member_amount_cents"]),
                } for item in pool_totals],
                "models": [{"model": model, "requests": int(requests or 0), "tokens": int(tokens or 0),
                            "cost": format_usd_nano(int(cost or 0))} for model, requests, tokens, cost in model_rows],
                "totals": {
                    "requests": sum(item["requests"] for item in rows),
                    "tokens": sum(item["tokens"] for item in rows),
                    "actual": format_usd_nano(sum(item["actual_nano"] for item in rows)),
                    "request_actual": format_usd_nano(sum(item["request_actual_nano"] for item in rows)),
                    "manual_actual": format_usd_nano(sum(item["manual_actual_nano"] for item in rows)),
                    "billed": format_usd_nano(sum(value[1] for value in live_user.values())),
                    "member_amount": format_cents(member_amount_cents),
                    "metered_amount": format_cents(metered_amount_cents),
                    "amount": format_cents(member_amount_cents + metered_amount_cents),
                    "fixed_cost": format_cents(sum(item["fixed_cost_cents"] for item in pool_totals)),
                },
            }

    def user_summary(self, user_id: int, cycle_name: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            if user is None or user.registered_at_ms is None:
                raise BillingError("user not found")
        billing = self.dashboard(cycle_name)
        billing_row = next(
            (row for row in billing["rows"] if row["telegram_user_id"] == user_id),
            None,
        )
        with self.db.session() as session:
            user = session.get(TelegramUser, user_id)
            cycle = session.scalar(
                select(BillingCycle).where(BillingCycle.name == billing["cycle"]["name"])
            ) if billing["cycle"] else None
            data: dict[str, Any] = {"telegram_user_id": user_id, "username": user.username, "first_name": user.first_name, "last_name": user.last_name,
                                    "statement": None if billing_row is None else {
                                        "actual": billing_row["actual"],
                                        "request_actual": billing_row["request_actual"],
                                        "manual_actual": billing_row["manual_actual"],
                                        "billed": billing_row["billed"],
                                        "amount": billing_row["amount"],
                                        "live": bool(billing["cycle"]["estimate_live"]),
                                        "generated_at": billing["cycle"]["estimate_generated_at"],
                                    },
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

    def request_filter_options(self, user_id: int | None, *, all_users: bool = False) -> dict[str, Any]:
        if (user_id is None) != all_users:
            raise BillingError("请求查询范围无效")
        event_scope = (
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
        scope_filters = [] if all_users else [KeyOwnershipPeriod.telegram_user_id == user_id]
        with self.db.session() as session:
            if not all_users and session.get(TelegramUser, user_id) is None:
                raise BillingError("用户不存在")
            models = sorted({
                str(value)
                for row in session.execute(
                    select(
                        RawUsageEvent.model,
                        RawUsageEvent.requested_model,
                        RawUsageEvent.resolved_model,
                    ).select_from(event_scope).where(*scope_filters)
                )
                for value in row
                if value
            })
            tiers = sorted({str(value or "default") for value in session.scalars(
                select(RawUsageEvent.service_tier).select_from(event_scope)
                .where(*scope_filters).distinct().order_by(RawUsageEvent.service_tier)
            )})
            providers = [str(value) for value in session.scalars(
                select(RawUsageEvent.provider).select_from(event_scope)
                .where(*scope_filters).where(RawUsageEvent.provider.is_not(None))
                .distinct().order_by(RawUsageEvent.provider)
            )]
            failure_codes = [int(value) for value in session.scalars(
                select(RawUsageEvent.fail_status_code).select_from(event_scope)
                .where(*scope_filters).where(RawUsageEvent.fail_status_code.is_not(None))
                .distinct().order_by(RawUsageEvent.fail_status_code)
            )]
            bounds = session.execute(
                select(func.min(RawUsageEvent.occurred_at_ms), func.max(RawUsageEvent.occurred_at_ms))
                .select_from(event_scope).where(*scope_filters)
            ).one()
            key_rows = session.execute(
                select(APIKey.id, APIKey.masked_value, APIKey.display_name, APIKey.status)
                .select_from(event_scope)
                .where(*scope_filters).where(APIKey.id.is_not(None))
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
        user_id: int | None,
        *,
        all_users: bool = False,
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
        min_tps: float | None = None,
        max_tps: float | None = None,
        long_context: bool | None = None,
        query_text: str | None = None,
        sort: str = "time_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if (user_id is None) != all_users:
            raise BillingError("请求查询范围无效")
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
        for value in (min_tps, max_tps):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise BillingError("TPS 筛选值必须是非负有限数")
        if min_tps is not None and max_tps is not None and min_tps > max_tps:
            raise BillingError("TPS 筛选下限不能大于上限")

        with self.db.session() as session:
            active_version_id = self._active_pricing_id(session)
            cycle_pricing_version = (
                select(BillingCycle.pricing_version_id)
                .where(
                    BillingCycle.start_at_ms <= RawUsageEvent.occurred_at_ms,
                    BillingCycle.end_at_ms > RawUsageEvent.occurred_at_ms,
                )
                .order_by(BillingCycle.start_at_ms.desc(), BillingCycle.id.desc())
                .limit(1)
                .correlate(RawUsageEvent)
                .scalar_subquery()
            )
            effective_version_id = func.coalesce(cycle_pricing_version, active_version_id)
            generation_ms_expression = RawUsageEvent.latency_ms - RawUsageEvent.ttft_ms
            tps_expression = case(
                (
                    and_(
                        RawUsageEvent.output_tokens > 0,
                        RawUsageEvent.latency_ms.is_not(None),
                        RawUsageEvent.ttft_ms.is_not(None),
                        generation_ms_expression > 0,
                    ),
                    RawUsageEvent.output_tokens * 1000.0 / generation_ms_expression,
                ),
                else_=None,
            )
            event_history = (
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
                .outerjoin(TelegramUser.__table__, TelegramUser.telegram_user_id == KeyOwnershipPeriod.telegram_user_id)
                .outerjoin(RatedEvent.__table__, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == effective_version_id,
                ))
            )
            filters: list[Any] = [] if all_users else [KeyOwnershipPeriod.telegram_user_id == user_id]
            if since_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms >= since_ms)
            if until_ms is not None:
                filters.append(RawUsageEvent.occurred_at_ms < until_ms)
            if models:
                selected_models = [value for value in models if value]
                filters.append(or_(
                    RawUsageEvent.model.in_(selected_models),
                    RawUsageEvent.requested_model.in_(selected_models),
                    RawUsageEvent.resolved_model.in_(selected_models),
                ))
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
            if min_tps is not None:
                filters.append(tps_expression >= min_tps)
            if max_tps is not None:
                filters.append(tps_expression <= max_tps)
            if long_context is not None:
                filters.append(RatedEvent.long_context_applied.is_(long_context))
            if query_text and query_text.strip():
                escaped = query_text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                filters.append(or_(
                    RawUsageEvent.request_id.ilike(pattern, escape="\\"),
                    RawUsageEvent.model.ilike(pattern, escape="\\"),
                    RawUsageEvent.requested_model.ilike(pattern, escape="\\"),
                    RawUsageEvent.resolved_model.ilike(pattern, escape="\\"),
                ))

            sort_options = {
                "time_desc": RawUsageEvent.occurred_at_ms.desc(),
                "time_asc": RawUsageEvent.occurred_at_ms.asc(),
                "tokens_desc": RawUsageEvent.total_tokens.desc(),
                "cost_desc": RatedEvent.rated_weight_nano_usd.desc(),
                "latency_desc": RawUsageEvent.latency_ms.desc(),
                "ttft_desc": RawUsageEvent.ttft_ms.desc(),
                "tps_desc": tps_expression.desc(),
            }
            if sort not in sort_options:
                raise BillingError("排序方式无效")

            aggregate = session.execute(
                select(
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RawUsageEvent.input_tokens),
                    func.sum(RawUsageEvent.output_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                    func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    func.sum(case((RatedEvent.id.is_(None), 1), else_=0)),
                ).select_from(event_history).where(*filters)
            ).one()
            total = int(aggregate[0] or 0)
            rows = session.execute(
                select(RawUsageEvent, RatedEvent, APIKey, TelegramUser)
                .select_from(event_history)
                .where(*filters)
                .order_by(sort_options[sort], RawUsageEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            items = []
            for event, rated, key, owner in rows:
                generation_ms = None
                tps = None
                if event.latency_ms is not None and event.ttft_ms is not None:
                    candidate_generation_ms = int(event.latency_ms) - int(event.ttft_ms)
                    if candidate_generation_ms > 0:
                        generation_ms = candidate_generation_ms
                        if event.output_tokens > 0:
                            tps = round(int(event.output_tokens) * 1000 / candidate_generation_ms, 2)
                key_payload = {
                    "id": key.id if key else None,
                    "masked": key.masked_value if key else mask_hash(event.api_key_hash or ""),
                    "name": key.display_name if key else None,
                }
                item = {
                    "id": event.id,
                    "request_id": event.request_id,
                    "occurred_at_ms": event.occurred_at_ms,
                    "occurred_at": self._iso_timestamp(event.occurred_at_ms),
                    "provider": event.provider,
                    "model": event.model,
                    "requested_model": event.requested_model,
                    "resolved_model": event.resolved_model,
                    "reasoning_effort": event.reasoning_effort or None,
                    "service_tier": rated.service_tier if rated else (event.service_tier or "default"),
                    "key": key_payload,
                    "tokens": {
                        "input": event.input_tokens,
                        "cache_read": self._effective_cache_read_tokens(event),
                        "cache_creation": event.cache_creation_tokens,
                        "output": event.output_tokens,
                        "reasoning": event.reasoning_tokens,
                        "total": event.total_tokens,
                    },
                    "failed": bool(event.failed),
                    "status_code": event.fail_status_code,
                    "latency_ms": event.latency_ms,
                    "ttft_ms": event.ttft_ms,
                    "generation_ms": generation_ms,
                    "tps": tps,
                    "long_context": bool(rated.long_context_applied) if rated else None,
                    "cost_nano_usd": rated.rated_weight_nano_usd if rated else None,
                    "cost": format_usd_nano(rated.rated_weight_nano_usd) if rated else None,
                    "pricing_status": "priced" if rated else "unpriced",
                }
                if all_users:
                    item["owner"] = ({
                        "telegram_user_id": owner.telegram_user_id,
                        "name": self._user_name(owner, owner.telegram_user_id),
                    } if owner else None)
                items.append(item)
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
                    "input_tokens": int(aggregate[2] or 0),
                    "output_tokens": int(aggregate[3] or 0),
                    "cost_nano_usd": int(aggregate[4] or 0),
                    "cost": format_usd_nano(int(aggregate[4] or 0)),
                    "failed": int(aggregate[5] or 0),
                    "unpriced": int(aggregate[6] or 0),
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
                .where(*filters, KeyOwnershipPeriod.telegram_user_id.is_not(None))
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
            unowned_rows = session.execute(
                select(
                    RawUsageEvent.api_key_hash,
                    APIKey.id,
                    APIKey.masked_value,
                    APIKey.display_name,
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                    func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    func.sum(case((RatedEvent.long_context_applied.is_(True), 1), else_=0)),
                )
                .select_from(ranking_source)
                .where(*filters, KeyOwnershipPeriod.telegram_user_id.is_(None))
                .group_by(
                    RawUsageEvent.api_key_hash,
                    APIKey.id,
                    APIKey.masked_value,
                    APIKey.display_name,
                )
            ).all()
            for key_hash, key_id, masked, display_name, requests, tokens, cost, failed, long_context in unowned_rows:
                values = {
                    "requests": int(requests or 0),
                    "tokens": int(tokens or 0),
                    "cost_nano_usd": int(cost or 0),
                    "failed": int(failed or 0),
                    "long_context": int(long_context or 0),
                }
                key_label = display_name or masked or (mask_hash(str(key_hash)) if key_hash else "未知 API Key")
                rows.append({
                    "telegram_user_id": None,
                    "api_key_id": key_id,
                    "name": key_label,
                    **values,
                    "cost": format_usd_nano(values["cost_nano_usd"]),
                    "success_rate": round((values["requests"] - values["failed"]) * 100 / values["requests"], 2)
                    if values["requests"] else None,
                    "key_count": 1,
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
            active_version_id = self._active_pricing_id(session)
            active_version = session.get(PricingVersion, active_version_id)
            cycle = self._display_cycle(session, cycle_name)
            if cycle_name and cycle is None:
                raise BillingError("账期不存在")
            version_id = cycle.pricing_version_id if cycle else active_version_id
            version = session.get(PricingVersion, version_id)
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
            unpriced_filters: list[Any] = [RatedEvent.id.is_(None)]
            if cycle:
                unpriced_filters.extend([
                    RawUsageEvent.occurred_at_ms >= cycle.start_at_ms,
                    RawUsageEvent.occurred_at_ms < cycle.end_at_ms,
                ])
            unpriced = int(session.scalar(
                select(func.count()).select_from(RawUsageEvent)
                .outerjoin(RatedEvent, and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ))
                .where(*unpriced_filters)
            ) or 0)
            gradient = session.get(GradientRule, cycle.gradient_rule_id) if cycle else None
            def version_payload(item: PricingVersion, unpriced_events: int | None = None) -> dict[str, Any]:
                payload = {
                    "id": item.id,
                    "name": item.name,
                    "status": item.status,
                    "source": item.source,
                    "activated_at": self._iso_timestamp(item.activated_at_ms),
                }
                if unpriced_events is not None:
                    payload["unpriced_events"] = unpriced_events
                return payload
            return {
                "active_version": version_payload(active_version),
                "selected_version": version_payload(version, unpriced),
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
                    "configured": {
                        "input": bool(rule.input_configured),
                        "output": bool(rule.output_configured),
                        "cache_read": bool(rule.cache_read_configured),
                        "cache_creation": bool(rule.cache_creation_configured),
                    },
                    "priority_multiplier_ppm": _priority_multiplier_ppm(rule.model),
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
                        "pricing_version_id": cycle.pricing_version_id,
                        "gradient_rule_id": cycle.gradient_rule_id,
                        "gradient_rule": gradient.name if gradient else None,
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
                        "unowned_keys_without_multiplier_are_billed": False,
                        "unowned_metered_keys_use_cost_multiplier": True,
                        "metered_keys_reduce_pool_fixed_cost_before_allocation": True,
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
                "window_usage_tokens": None,
                "window_usage_cost": None,
            })
        credits = payload.get("rateLimitResetCreditsAvailableCount")
        return rows, int(credits) if isinstance(credits, (int, float)) else None

    def _external_timestamp_ms(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            numeric = int(value)
            return numeric if numeric > 10_000_000_000 else numeric * 1000
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return int(parsed.timestamp() * 1000)

    def _stable_quota_window_start(self, account_id: str, quota_key: str, candidate_ms: int | None) -> int | None:
        if candidate_ms is None:
            return None
        cache_key = (account_id, quota_key)
        with self._quota_window_lock:
            current = self._quota_window_starts.get(cache_key)
            if current is None or abs(candidate_ms - current) >= 5 * 60_000:
                self._quota_window_starts[cache_key] = candidate_ms
                return candidate_ms
            return current

    def _account_usage_aggregate(
        self,
        session: Any,
        version_id: int,
        auth_index: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        model_metric: str | None = None,
    ) -> dict[str, Any]:
        filters: list[Any] = [RawUsageEvent.auth_index == auth_index]
        if start_ms is not None:
            filters.append(RawUsageEvent.occurred_at_ms >= start_ms)
        if end_ms is not None:
            filters.append(RawUsageEvent.occurred_at_ms < end_ms)
        if model_metric:
            metric = _model_slug(model_metric)
            model_columns = (
                RawUsageEvent.model,
                RawUsageEvent.requested_model,
                RawUsageEvent.resolved_model,
            )
            model_filters = []
            for column in model_columns:
                model_filters.extend((
                    func.lower(func.coalesce(column, "")) == metric,
                    func.lower(func.coalesce(column, "")).like(f"%/{metric}"),
                ))
            filters.append(or_(*model_filters))
        compatible_cached = func.max(
            func.max(RawUsageEvent.cached_tokens, RawUsageEvent.cache_tokens)
            - func.max(RawUsageEvent.cache_read_tokens, 0)
            - func.max(RawUsageEvent.cache_creation_tokens, 0),
            0,
        )
        row = session.execute(
            select(
                func.count(RawUsageEvent.id),
                func.sum(case((RawUsageEvent.failed.is_(False), 1), else_=0)),
                func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                func.sum(RawUsageEvent.input_tokens),
                func.sum(RawUsageEvent.output_tokens),
                func.sum(RawUsageEvent.reasoning_tokens),
                func.sum(compatible_cached),
                func.sum(RawUsageEvent.cache_read_tokens),
                func.sum(RawUsageEvent.cache_creation_tokens),
                func.sum(RawUsageEvent.total_tokens),
                func.sum(RatedEvent.rated_weight_nano_usd),
                func.sum(case((RatedEvent.id.is_(None), 1), else_=0)),
                func.min(RawUsageEvent.occurred_at_ms),
                func.max(RawUsageEvent.occurred_at_ms),
            )
            .select_from(RawUsageEvent)
            .outerjoin(
                RatedEvent,
                and_(
                    RatedEvent.raw_event_id == RawUsageEvent.id,
                    RatedEvent.pricing_version_id == version_id,
                ),
            )
            .where(*filters)
        ).one()
        requests = int(row[0] or 0)
        success = int(row[1] or 0)
        cost_nano = int(row[10] or 0)
        return {
            "requests": requests,
            "success": success,
            "failed": int(row[2] or 0),
            "success_rate": round(success * 100 / requests, 2) if requests else None,
            "input_tokens": int(row[3] or 0),
            "output_tokens": int(row[4] or 0),
            "reasoning_tokens": int(row[5] or 0),
            "cached_tokens": int(row[6] or 0),
            "cache_read_tokens": int(row[7] or 0),
            "cache_creation_tokens": int(row[8] or 0),
            "total_tokens": int(row[9] or 0),
            "cost_nano_usd": cost_nano,
            "cost": format_usd_nano(cost_nano),
            "unpriced": int(row[11] or 0),
            "first_used_at": self._iso_timestamp(row[12]),
            "last_used_at": self._iso_timestamp(row[13]),
            "source": "billing-panel",
        }

    def _hydrate_account_usage(self, accounts: list[dict[str, Any]], auth_by_account: dict[str, str]) -> None:
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
        self._rate_until_current(version_id)
        current = now_ms()
        with self.db.session() as session:
            for account in accounts:
                auth_index = auth_by_account.get(str(account["id"]))
                if not auth_index:
                    continue
                account["usage"] = self._account_usage_aggregate(session, version_id, auth_index)
                for quota in account["quota"]:
                    window_seconds = int(quota.get("window_seconds") or 0)
                    reset_at_ms = self._external_timestamp_ms(quota.get("reset_at"))
                    if reset_at_ms is None and quota.get("reset_after_seconds") is not None:
                        reset_at_ms = current + int(quota["reset_after_seconds"] or 0) * 1000
                    window_end_ms = min(current, reset_at_ms) if reset_at_ms is not None else current
                    if reset_at_ms is not None and window_seconds > 0:
                        candidate_start_ms = reset_at_ms - window_seconds * 1000
                    else:
                        candidate_start_ms = current - window_seconds * 1000 if window_seconds > 0 else None
                    window_start_ms = self._stable_quota_window_start(
                        str(account["id"]),
                        str(quota.get("key") or quota.get("label") or "unknown"),
                        candidate_start_ms,
                    )
                    model_metric = quota.get("metric") if quota.get("scope") == "additional" else None
                    usage = self._account_usage_aggregate(
                        session,
                        version_id,
                        auth_index,
                        start_ms=window_start_ms,
                        end_ms=window_end_ms + 1,
                        model_metric=model_metric,
                    )
                    quota["window_started_at"] = self._iso_timestamp(window_start_ms)
                    quota["window_ended_at"] = self._iso_timestamp(window_end_ms)
                    quota["window_usage_requests"] = usage["requests"]
                    quota["window_usage_tokens"] = usage["total_tokens"]
                    quota["window_usage_cost"] = usage["cost"]
                    quota["window_unpriced"] = usage["unpriced"]
                    quota["usage_source"] = "billing-panel"

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
                    "requests": 0,
                    "success": 0,
                    "failed": 0,
                    "success_rate": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cached_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "total_tokens": 0,
                    "cost_nano_usd": 0,
                    "cost": "0.0000",
                    "unpriced": 0,
                    "first_used_at": None,
                    "last_used_at": None,
                    "source": "billing-panel",
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
        accounts, account_by_auth, auth_by_account = self._sanitize_accounts(raw)
        self._hydrate_account_usage(accounts, auth_by_account)
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
        for item in (raw.get("tasks") or []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            account_id = account_by_auth.get(str(item.get("authIndex") or ""))
            if account_id:
                tasks.append({"account_id": account_id, "status": "queued"})
        rejected = []
        for item in (raw.get("rejected") or []) if isinstance(raw, dict) else []:
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

    def _status_range_bounds(self, range_name: str, start: str | None, end: str | None) -> tuple[int, int]:
        zone = ZoneInfo(self.settings.timezone)
        current = datetime.now(zone)
        today = current.replace(hour=0, minute=0, second=0, microsecond=0)
        if range_name == "today":
            start_dt, end_dt = today, current
        elif range_name == "yesterday":
            start_dt, end_dt = today - timedelta(days=1), today
        elif range_name == "24h":
            start_dt, end_dt = current - timedelta(hours=24), current
        elif range_name == "7d":
            start_dt, end_dt = current - timedelta(days=7), current
        elif range_name == "30d":
            start_dt, end_dt = current - timedelta(days=30), current
        elif range_name == "custom":
            start_ms = self._parse_filter_time(start)
            end_ms = self._parse_filter_time(end, end_of_date=True)
            if start_ms is None or end_ms is None or start_ms >= end_ms:
                raise BillingError("自定义状态范围需要有效的开始和结束时间")
            return start_ms, end_ms
        else:
            raise BillingError("全站状态时间范围无效")
        return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
        return int(ordered[index])

    def _local_overview(self, version_id: int, start_ms: int, end_ms: int) -> dict[str, Any]:
        with self.db.session() as session:
            aggregate = session.execute(
                select(
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                    func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    func.sum(case((RatedEvent.id.is_(None), 1), else_=0)),
                )
                .select_from(RawUsageEvent)
                .outerjoin(
                    RatedEvent,
                    and_(
                        RatedEvent.raw_event_id == RawUsageEvent.id,
                        RatedEvent.pricing_version_id == version_id,
                    ),
                )
                .where(
                    RawUsageEvent.occurred_at_ms >= start_ms,
                    RawUsageEvent.occurred_at_ms < end_ms,
                )
            ).one()
            duration_minutes = max((end_ms - start_ms) / 60_000, 1)
            requests = int(aggregate[0] or 0)
            tokens = int(aggregate[1] or 0)
            failed = int(aggregate[3] or 0)
            block_count = 48
            block_ms = max(1, math.ceil((end_ms - start_ms) / block_count))
            bucket = cast((RawUsageEvent.occurred_at_ms - start_ms) / block_ms, Integer)
            bucket_rows = {
                int(index): (int(success or 0), int(failure or 0))
                for index, success, failure in session.execute(
                    select(
                        bucket,
                        func.sum(case((RawUsageEvent.failed.is_(False), 1), else_=0)),
                        func.sum(case((RawUsageEvent.failed.is_(True), 1), else_=0)),
                    )
                    .where(
                        RawUsageEvent.occurred_at_ms >= start_ms,
                        RawUsageEvent.occurred_at_ms < end_ms,
                    )
                    .group_by(bucket)
                )
            }
        details = []
        for index in range(block_count):
            success, failure = bucket_rows.get(index, (0, 0))
            total = success + failure
            details.append({
                "start_time": self._iso_timestamp(start_ms + index * block_ms),
                "success": success,
                "failure": failure,
                "rate": round(success * 100 / total, 3) if total else -1,
            })
        success = requests - failed
        cost_nano = int(aggregate[2] or 0)
        unpriced = int(aggregate[4] or 0)
        return {
            "summary": {
                "request_count": requests,
                "token_count": tokens,
                "rpm": round(requests / duration_minutes, 2),
                "tpm": round(tokens / duration_minutes, 2),
                "total_cost": format_usd_nano(cost_nano),
                "cost_available": True,
                "cost_complete": unpriced == 0,
                "unpriced_events": unpriced,
                "source": "billing-panel",
            },
            "service_health": {
                "total_success": success,
                "total_failure": failed,
                "success_rate": round(success * 100 / requests, 3) if requests else None,
                "block_details": details,
                "source": "billing-panel",
            },
        }

    @staticmethod
    def _usage_rows(items: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        total_cost = sum(item["cost_nano"] for item in items.values())
        rows = []
        for label, item in items.items():
            rows.append({
                "label": label,
                "requests": item["requests"],
                "tokens": item["tokens"],
                "cost": format_usd_nano(item["cost_nano"]),
                "share": round(item["cost_nano"] * 100 / total_cost, 2) if total_cost else 0,
            })
        rows.sort(key=lambda item: (Decimal(item["cost"].replace(",", "")), item["requests"]), reverse=True)
        return rows

    def _local_realtime(self, version_id: int, window: str) -> dict[str, Any]:
        window_minutes = int(window.removesuffix("m"))
        end_ms = now_ms()
        start_ms = end_ms - window_minutes * 60_000
        bucket_ms = 60_000
        bucket_count = window_minutes
        with self.db.session() as session:
            rows = session.execute(
                select(RawUsageEvent, RatedEvent)
                .outerjoin(
                    RatedEvent,
                    and_(
                        RatedEvent.raw_event_id == RawUsageEvent.id,
                        RatedEvent.pricing_version_id == version_id,
                    ),
                )
                .where(
                    RawUsageEvent.occurred_at_ms >= start_ms,
                    RawUsageEvent.occurred_at_ms < end_ms,
                )
                .order_by(RawUsageEvent.occurred_at_ms)
            ).all()
        buckets = [{
            "tokens": 0,
            "requests": 0,
            "success": 0,
            "failure": 0,
            "cached": 0,
            "ttft": [],
            "latency": [],
        } for _ in range(bucket_count)]
        models: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "tokens": 0, "cost_nano": 0})
        accounts: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "tokens": 0, "cost_nano": 0})
        providers: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "tokens": 0, "cost_nano": 0})
        key_hashes: set[str] = set()
        key_requests = 0
        key_tokens = 0
        key_cost_nano = 0
        for event, rated in rows:
            index = min(bucket_count - 1, max(0, (event.occurred_at_ms - start_ms) // bucket_ms))
            item = buckets[index]
            item["tokens"] += int(event.total_tokens or 0)
            item["requests"] += 1
            item["failure" if event.failed else "success"] += 1
            item["cached"] += self._compatible_cached_tokens(event) + int(event.cache_read_tokens or 0)
            if event.ttft_ms is not None:
                item["ttft"].append(int(event.ttft_ms))
            if event.latency_ms is not None:
                item["latency"].append(int(event.latency_ms))
            cost_nano = int(rated.rated_weight_nano_usd) if rated else 0
            model = event.resolved_model or event.requested_model or event.model or "未知模型"
            account = event.account_snapshot or "未标记账号"
            provider = event.provider or "未知 Provider"
            for target, label in ((models, model), (accounts, account), (providers, provider)):
                target[label]["requests"] += 1
                target[label]["tokens"] += int(event.total_tokens or 0)
                target[label]["cost_nano"] += cost_nano
            if event.api_key_hash:
                key_hashes.add(event.api_key_hash)
            key_requests += 1
            key_tokens += int(event.total_tokens or 0)
            key_cost_nano += cost_nano
        token_velocity = []
        response_level = []
        request_level = []
        cache_level = []
        for index, item in enumerate(buckets):
            bucket_at = self._iso_timestamp(start_ms + index * bucket_ms)
            token_velocity.append({"bucket": bucket_at, "tokens_per_minute": item["tokens"]})
            response_level.append({
                "bucket": bucket_at,
                "ttft_p50_ms": self._percentile(item["ttft"], 0.50),
                "ttft_p95_ms": self._percentile(item["ttft"], 0.95),
                "latency_p50_ms": self._percentile(item["latency"], 0.50),
                "latency_p95_ms": self._percentile(item["latency"], 0.95),
            })
            request_level.append({
                "bucket": bucket_at,
                "requests": item["requests"],
                "success": item["success"],
                "failure": item["failure"],
            })
            cache_level.append({"bucket": bucket_at, "cached_tokens": item["cached"]})
        return {
            "window": window,
            "timezone": self.settings.timezone,
            "bucket_seconds": 60,
            "window_start": self._iso_timestamp(start_ms),
            "window_end": self._iso_timestamp(end_ms),
            "token_velocity": token_velocity,
            "response_level": response_level,
            "request_level": request_level,
            "cache_level": cache_level,
            "response_distribution": {
                "ttft": {"average_line": [], "total_particles": 0, "sampled": False, "max_particles": 0},
                "latency": {"average_line": [], "total_particles": 0, "sampled": False, "max_particles": 0},
            },
            "current_usage": {
                "models": self._usage_rows(models),
                "api_keys": {
                    "count": len(key_hashes),
                    "requests": key_requests,
                    "tokens": key_tokens,
                    "cost": format_usd_nano(key_cost_nano),
                },
                "upstream_accounts": self._usage_rows(accounts),
                "ai_providers": self._usage_rows(providers),
            },
            "source": "billing-panel",
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
        range_start_ms, range_end_ms = self._status_range_bounds(range_name, start, end)
        realtime_start_ms = now_ms() - int(window.removesuffix("m")) * 60_000
        with self.db.session() as session:
            version_id = self._active_pricing_id(session)
        self._rate_until_current(version_id, min(range_start_ms, realtime_start_ms), max(range_end_ms, now_ms()))
        overview = self._local_overview(version_id, range_start_ms, range_end_ms)
        realtime = self._local_realtime(version_id, window)
        keeper: dict[str, Any]
        errors: list[str] = []
        try:
            raw = self.keeper.status_snapshot(range_name, window, start, end)
            keeper = {
                "available": True,
                "status": raw.get("status", {}),
                "version": raw.get("version", {}),
                "update": raw.get("update", {}),
                "overview": overview,
                "realtime": realtime,
                "usage_source": "billing-panel",
            }
        except (httpx.HTTPError, BillingDependencyError):
            keeper = {
                "available": False,
                "error": "Keeper 状态接口不可用",
                "overview": overview,
                "realtime": realtime,
                "usage_source": "billing-panel",
            }
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

    @staticmethod
    def _normalize_manual_usage_input(amount_nano_usd: int, reason: str) -> str:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BillingError("手动原始用量必须填写原因")
        if amount_nano_usd == 0:
            raise BillingError("手动原始用量不能为零")
        if abs(amount_nano_usd) > 9_223_372_036_854_775_807:
            raise BillingError("手动原始用量超出可记录范围")
        return normalized_reason

    @staticmethod
    def _manual_usage_balance(
        session: Any,
        cycle_id: int,
        pool_id: int,
        user_id: int,
        exclude_id: int | None = None,
    ) -> int:
        query = select(func.sum(ManualUsageAdjustment.amount_nano_usd)).where(
            ManualUsageAdjustment.cycle_id == cycle_id,
            ManualUsageAdjustment.pool_id == pool_id,
            ManualUsageAdjustment.telegram_user_id == user_id,
        )
        if exclude_id is not None:
            query = query.where(ManualUsageAdjustment.id != exclude_id)
        return int(session.scalar(query) or 0)

    @staticmethod
    def _manual_usage_state(row: ManualUsageAdjustment, cycle_name: str) -> dict[str, Any]:
        return {
            "cycle": cycle_name,
            "pool_id": row.pool_id,
            "telegram_user_id": row.telegram_user_id,
            "amount_nano_usd": row.amount_nano_usd,
            "amount_usd": format(Decimal(row.amount_nano_usd) / Decimal(NANO_USD), "f"),
            "reason": row.reason,
        }

    @staticmethod
    def _manual_usage_target(
        session: Any,
        cycle_name: str,
        pool_id: int,
        user_id: int,
    ) -> BillingCycle:
        cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == cycle_name))
        if cycle is None or cycle.status == "closed":
            raise BillingError("账期不存在或已经关闭")
        user = session.get(TelegramUser, user_id)
        if user is None or user.registered_at_ms is None:
            raise BillingError("Telegram 用户不存在或尚未注册")
        pool = session.get(ResourcePool, pool_id)
        configured = session.get(CyclePoolCost, {"cycle_id": cycle.id, "pool_id": pool_id})
        if pool is None or configured is None:
            raise BillingError("资源池未配置到该账期")
        return cycle

    def add_manual_usage_adjustment(
        self,
        cycle_name: str,
        pool_id: int,
        user_id: int,
        amount_nano_usd: int,
        reason: str,
        operator_id: int | None,
        operator_type: str = "telegram",
    ) -> int:
        normalized_reason = self._normalize_manual_usage_input(amount_nano_usd, reason)
        with self._manual_usage_lock, self.db.session() as session:
            cycle = self._manual_usage_target(session, cycle_name, pool_id, user_id)
            current_manual = self._manual_usage_balance(session, cycle.id, pool_id, user_id)
            if current_manual + amount_nano_usd < 0:
                raise BillingError("冲销金额不能超过该用户在此资源池的手动原始用量")
            created_at = now_ms()
            row = ManualUsageAdjustment(
                cycle_id=cycle.id,
                pool_id=pool_id,
                telegram_user_id=user_id,
                amount_nano_usd=amount_nano_usd,
                reason=normalized_reason,
                operator_user_id=operator_id,
                created_at_ms=created_at,
            )
            session.add(row)
            session.flush()
            self._invalidate_cycle_previews(session, [cycle.id])
            session.add(AuditLog(
                operator_type=operator_type,
                operator_id=str(operator_id if operator_id is not None else "admin-token"),
                operation="manual-usage.create",
                target=str(row.id),
                after_json=json.dumps(self._manual_usage_state(row, cycle.name)),
                reason=normalized_reason,
                created_at_ms=created_at,
            ))
            return row.id

    def update_manual_usage_adjustment(
        self,
        adjustment_id: int,
        cycle_name: str,
        pool_id: int,
        user_id: int,
        amount_nano_usd: int,
        reason: str,
        operator_id: int | None,
        operator_type: str = "telegram",
    ) -> int:
        normalized_reason = self._normalize_manual_usage_input(amount_nano_usd, reason)
        with self._manual_usage_lock, self.db.session() as session:
            row = session.get(ManualUsageAdjustment, adjustment_id)
            if row is None:
                raise BillingError("补录记录不存在")
            source_cycle = session.get(BillingCycle, row.cycle_id)
            if source_cycle is None or source_cycle.status == "closed":
                raise BillingError("已关闭账期的补录不能修改")
            target_cycle = self._manual_usage_target(session, cycle_name, pool_id, user_id)
            before = self._manual_usage_state(row, source_cycle.name)
            source_group = (row.cycle_id, row.pool_id, row.telegram_user_id)
            target_group = (target_cycle.id, pool_id, user_id)
            source_without = self._manual_usage_balance(session, *source_group, exclude_id=row.id)
            if source_group != target_group and source_without < 0:
                raise BillingError("该补录已有后续冲销，不能移动到其他账期、资源池或用户")
            target_without = self._manual_usage_balance(session, *target_group, exclude_id=row.id)
            if target_without + amount_nano_usd < 0:
                raise BillingError("更新后会导致目标补录余额为负数")
            if (
                source_group == target_group
                and row.amount_nano_usd == amount_nano_usd
                and row.reason == normalized_reason
            ):
                raise BillingError("补录信息没有变化")
            changed_at = now_ms()
            row.cycle_id = target_cycle.id
            row.pool_id = pool_id
            row.telegram_user_id = user_id
            row.amount_nano_usd = amount_nano_usd
            row.reason = normalized_reason
            row.updated_at_ms = changed_at
            self._invalidate_cycle_previews(session, sorted({source_cycle.id, target_cycle.id}))
            session.add(AuditLog(
                operator_type=operator_type,
                operator_id=str(operator_id if operator_id is not None else "admin-token"),
                operation="manual-usage.update",
                target=str(row.id),
                before_json=json.dumps(before),
                after_json=json.dumps(self._manual_usage_state(row, target_cycle.name)),
                reason=normalized_reason,
                created_at_ms=changed_at,
            ))
            return row.id

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
            gradients = list(session.scalars(select(GradientRule).order_by(GradientRule.active.desc(), GradientRule.id)))
            gradient_map = {rule.id: rule for rule in gradients}
            pricing_versions = list(session.scalars(select(PricingVersion).order_by(PricingVersion.id.desc())))
            pricing_map = {version.id: version for version in pricing_versions}
            pools = list(session.scalars(select(ResourcePool).order_by(ResourcePool.id)))
            assignments = list(session.scalars(
                select(PoolAssignmentRule).order_by(PoolAssignmentRule.priority, PoolAssignmentRule.id)
            ))
            cycle_pool_costs: dict[int, list[dict[str, Any]]] = defaultdict(list)
            pool_map = {pool.id: pool for pool in pools}
            for item in session.scalars(select(CyclePoolCost).order_by(CyclePoolCost.cycle_id, CyclePoolCost.pool_id)):
                cycle_pool_costs[item.cycle_id].append({
                    "pool_id": item.pool_id,
                    "pool": pool_map[item.pool_id].name if item.pool_id in pool_map else str(item.pool_id),
                    "fixed_cost_cents": int(item.fixed_cost_cents),
                    "fixed_cost": format_cents(int(item.fixed_cost_cents)),
                })
            costs = {cycle_id: int(total or 0) for cycle_id, total in session.execute(
                select(CyclePoolCost.cycle_id, func.sum(CyclePoolCost.fixed_cost_cents)).group_by(CyclePoolCost.cycle_id)
            )}
            return {
                "cycles": [{"id": cycle.id, "name": cycle.name, "start": self._format_timestamp(cycle.start_at_ms),
                            "end": self._format_timestamp(cycle.end_at_ms), "status": cycle.status,
                            "waiver": cycle.data_quality_waiver,
                            "pricing_version_id": cycle.pricing_version_id,
                            "pricing_version": pricing_map[cycle.pricing_version_id].name if cycle.pricing_version_id in pricing_map else None,
                            "gradient_rule_id": cycle.gradient_rule_id,
                            "gradient_rule": gradient_map[cycle.gradient_rule_id].name if cycle.gradient_rule_id in gradient_map else None,
                            "pool_costs": cycle_pool_costs.get(cycle.id, []),
                            "fixed_cost_cents": costs.get(cycle.id, 0),
                            "fixed_cost": format_cents(costs.get(cycle.id, 0))}
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
                           "is_admin": bool(user.is_admin or user.telegram_user_id in self.settings.admin_user_ids),
                           "configured_admin": user.telegram_user_id in self.settings.admin_user_ids,
                           "active_keys": sum(1 for key in keys if key.current_owner_id == user.telegram_user_id and key.status == "active")}
                          for user in users.values()],
                "keys": [{"id": key.id, "masked": key.masked_value, "name": key.display_name,
                          "status": key.status, "owner_id": key.current_owner_id,
                          "owner": self._user_name(users.get(key.current_owner_id), key.current_owner_id or "未绑定"),
                          "present_in_cpa": bool(key.present_in_cpa),
                          "last_seen_in_cpa_at": self._iso_timestamp(key.last_seen_in_cpa_at_ms),
                          "billing_multiplier_ppm": key.billing_multiplier_ppm,
                          "billing_multiplier": None if key.billing_multiplier_ppm is None else format(
                              Decimal(key.billing_multiplier_ppm) / Decimal(1_000_000), "f"
                          ),
                          "billing_profile_editable": key.current_owner_id is None,
                          "created_at": self._format_timestamp(key.created_at_ms),
                          "revoked_at": self._format_timestamp(key.revoked_at_ms)} for key in keys],
                "ownership": [{"key_id": row.api_key_id,
                               "key": key_map[row.api_key_id].masked_value if row.api_key_id in key_map else str(row.api_key_id),
                               "user_id": row.telegram_user_id,
                               "user": self._user_name(users.get(row.telegram_user_id), row.telegram_user_id),
                               "from": self._format_timestamp(row.valid_from_ms), "to": self._format_timestamp(row.valid_to_ms),
                               "source": row.source, "reason": row.reason}
                              for row in session.scalars(select(KeyOwnershipPeriod).order_by(KeyOwnershipPeriod.valid_from_ms.desc()).limit(100))],
                "pools": [{
                    "id": pool.id,
                    "name": pool.name,
                    "active": pool.active,
                    "rules": [{
                        "id": rule.id,
                        "priority": rule.priority,
                        "auth_index_pattern": rule.auth_index_pattern,
                        "model_pattern": rule.model_pattern,
                        "active": rule.active,
                    } for rule in assignments if rule.pool_id == pool.id],
                } for pool in pools],
                "gradients": [{
                    "id": rule.id,
                    "name": rule.name,
                    "description": rule.description,
                    "tiers": json.loads(rule.tiers_json),
                    "active": rule.active,
                    "created_at": self._iso_timestamp(rule.created_at_ms),
                    "updated_at": self._iso_timestamp(rule.updated_at_ms),
                    "open_cycle_count": sum(
                        1 for cycle in cycles if cycle.gradient_rule_id == rule.id and cycle.status != "closed"
                    ),
                } for rule in gradients],
                "pricing": [{"id": p.id, "name": p.name, "status": p.status, "source": p.source,
                             "activated_at": self._iso_timestamp(p.activated_at_ms)} for p in pricing_versions],
                "adjustments": [{"cycle": next((cycle.name for cycle in cycles if cycle.id == row.cycle_id), str(row.cycle_id)),
                                 "user_id": row.telegram_user_id, "amount": format_cents(row.amount_cents),
                                 "reason": row.reason, "operator": row.operator_user_id,
                                 "at": self._format_timestamp(row.created_at_ms)}
                                for row in session.scalars(select(Adjustment).order_by(Adjustment.id.desc()).limit(100))],
                "manual_usage_adjustments": [{
                    "id": row.id,
                    "cycle": next((cycle.name for cycle in cycles if cycle.id == row.cycle_id), str(row.cycle_id)),
                    "pool_id": row.pool_id,
                    "pool": pool_map[row.pool_id].name if row.pool_id in pool_map else str(row.pool_id),
                    "user_id": row.telegram_user_id,
                    "user": self._user_name(users.get(row.telegram_user_id), row.telegram_user_id),
                    "amount_nano_usd": row.amount_nano_usd,
                    "amount_usd": format(Decimal(row.amount_nano_usd) / Decimal(NANO_USD), "f"),
                    "reason": row.reason,
                    "operator": row.operator_user_id,
                    "created_at": self._format_timestamp(row.created_at_ms),
                    "updated_at": None if row.updated_at_ms is None else self._format_timestamp(row.updated_at_ms),
                    "at": self._format_timestamp(row.updated_at_ms or row.created_at_ms),
                    "editable": next((cycle.status != "closed" for cycle in cycles if cycle.id == row.cycle_id), False),
                } for row in session.scalars(
                    select(ManualUsageAdjustment).order_by(ManualUsageAdjustment.id.desc())
                )],
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
                query.where(KeyOwnershipPeriod.telegram_user_id.is_not(None))
                .group_by(KeyOwnershipPeriod.telegram_user_id)
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
            unowned_filters = [KeyOwnershipPeriod.telegram_user_id.is_(None)]
            if since_ms is not None:
                unowned_filters.append(RawUsageEvent.occurred_at_ms >= since_ms)
            if until_ms is not None:
                unowned_filters.append(RawUsageEvent.occurred_at_ms < until_ms)
            for key_hash, key_id, masked, display_name, requests, tokens, cost in session.execute(
                select(
                    RawUsageEvent.api_key_hash,
                    APIKey.id,
                    APIKey.masked_value,
                    APIKey.display_name,
                    func.count(RawUsageEvent.id),
                    func.sum(RawUsageEvent.total_tokens),
                    func.sum(RatedEvent.rated_weight_nano_usd),
                )
                .select_from(ranking_source)
                .where(*unowned_filters)
                .group_by(
                    RawUsageEvent.api_key_hash,
                    APIKey.id,
                    APIKey.masked_value,
                    APIKey.display_name,
                )
            ):
                cost_nano = int(cost or 0)
                result.append({
                    "telegram_user_id": None,
                    "api_key_id": key_id,
                    "name": display_name or masked or (mask_hash(str(key_hash)) if key_hash else "未知 API Key"),
                    "requests": int(requests or 0),
                    "tokens": int(tokens or 0),
                    "cost": format_usd_nano(cost_nano),
                    "cost_nano": cost_nano,
                    "key_count": 1,
                })
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
                select(
                    KeyOwnershipPeriod.telegram_user_id,
                    APIKey.id,
                    APIKey.masked_value,
                    APIKey.display_name,
                    RawUsageEvent.api_key_hash,
                    RawUsageEvent.occurred_at_ms,
                    RawUsageEvent.total_tokens,
                )
                .select_from(ownership_source)
                .where(RawUsageEvent.occurred_at_ms >= start)
            ).all()
            users = {u.telegram_user_id: u for u in session.scalars(select(TelegramUser))}
        labels = [datetime.fromtimestamp((start + index * 3_600_000) / 1000, ZoneInfo(self.settings.timezone)).strftime("%m-%d %H") for index in range(hours + 1)]
        grouped: dict[tuple[str, int | str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        names: dict[tuple[str, int | str], str] = {}
        for user_id, key_id, masked, display_name, key_hash, occurred_at, tokens in rows:
            index = min(hours, max(0, int((int(occurred_at) - start) // 3_600_000)))
            if user_id is not None:
                group_key = ("user", int(user_id))
                user = users.get(user_id)
                names[group_key] = f"@{user.username}" if user and user.username else str(user_id)
            else:
                stable_key = int(key_id) if key_id is not None else str(key_hash or "unknown")
                group_key = ("key", stable_key)
                names[group_key] = display_name or masked or (mask_hash(str(key_hash)) if key_hash else "未知 API Key")
            grouped[group_key][labels[index]] += int(tokens or 0)
        series = []
        for group_key, values in grouped.items():
            series.append({"name": names[group_key], "values": dict(values), "total": sum(values.values())})
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
            self._invalidate_cycle_previews(session, [cycle.id])

    def list_cycles(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rules = {rule.id: rule.name for rule in session.scalars(select(GradientRule))}
            return [{"name": c.name, "start_at_ms": c.start_at_ms, "end_at_ms": c.end_at_ms, "status": c.status,
                     "waiver": c.data_quality_waiver, "gradient_rule_id": c.gradient_rule_id,
                     "gradient_rule": rules.get(c.gradient_rule_id)}
                    for c in session.scalars(select(BillingCycle).order_by(BillingCycle.start_at_ms.desc()))]

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
            checkpoint_state = session.execute(select(
                func.max(SyncCheckpoint.last_success_at_ms),
                func.sum(case((SyncCheckpoint.last_error.is_not(None), 1), else_=0)),
            )).one()
            sync_backlog = max(0, int(source_count) - int(raw_count))
            raw_excess = max(0, int(raw_count) - int(source_count))
            unpriced = max(0, int(raw_count) - int(rated_count))
            last_sync_at_ms = int(checkpoint_state[0]) if checkpoint_state[0] is not None else None
            stale_after_ms = max(60_000, int(self.settings.worker_interval_seconds * 5_000))
            sync_stale = last_sync_at_ms is None or now_ms() - last_sync_at_ms > stale_after_ms
            sync_has_error = int(checkpoint_state[1] or 0) > 0
            sync_degraded = sync_has_error or sync_backlog > 5_000 or (sync_backlog > 0 and sync_stale)
            integrity_ok = raw_excess == 0 and dead == 0 and unassigned == 0
            result = {
                "cpamp_events": int(source_count),
                "raw_events": int(raw_count),
                "rated_events": int(rated_count),
                "unpriced_events": unpriced,
                "dead_letters": int(dead),
                "unowned_events": int(unowned),
                "unassigned_events": int(unassigned),
                "sync_backlog": sync_backlog,
                "raw_excess": raw_excess,
                "sync_pending": sync_backlog > 0,
                "sync_stale": sync_stale,
                "sync_has_error": sync_has_error,
                "sync_degraded": sync_degraded,
                "last_sync_at": self._iso_timestamp(last_sync_at_ms),
                "rating_pending": unpriced > 0,
                "ok": integrity_ok,
                "settlement_ready": integrity_ok and sync_backlog == 0 and unpriced == 0,
            }
            if record:
                session.add(ReconciliationRun(cycle_id=None, result_json=json.dumps(result), ok=bool(result["ok"]), created_at_ms=now_ms()))
            return result

    @staticmethod
    def _canonical_tiers(items: list[dict[str, Any]]) -> list[dict[str, str | None]]:
        if not isinstance(items, list) or not items:
            raise BillingError("梯度规则至少需要一个区间")
        canonical: list[dict[str, str | None]] = []
        try:
            for item in items:
                left = Decimal(str(item["left"]))
                right = None if item.get("right") is None or str(item.get("right")).strip() == "" else Decimal(str(item["right"]))
                multiplier = Decimal(str(item["multiplier"]))
                if not left.is_finite() or (right is not None and not right.is_finite()) or not multiplier.is_finite():
                    raise ValueError
                canonical.append({
                    "left": format(left.normalize(), "f"),
                    "right": None if right is None else format(right.normalize(), "f"),
                    "multiplier": format(multiplier.normalize(), "f"),
                })
            parse_tiers(canonical)
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise BillingError("梯度区间必须从 0 开始、连续递增，且最后一段上限为空") from exc
        return canonical

    def create_gradient_rule(self, name: str, description: str | None, tiers: list[dict[str, Any]],
                             reason: str, operator_id: str = "admin-token") -> int:
        normalized_name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", normalized_name):
            raise BillingError("规则名称只能使用字母、数字、点、下划线或短横线")
        if not reason.strip():
            raise BillingError("创建梯度规则必须填写原因")
        canonical = self._canonical_tiers(tiers)
        with self.db.session() as session:
            if session.scalar(select(GradientRule).where(GradientRule.name == normalized_name)):
                raise BillingError("梯度规则名称已存在")
            created = now_ms()
            rule = GradientRule(
                name=normalized_name,
                description=(description or "").strip()[:300] or None,
                tiers_json=json.dumps(canonical, separators=(",", ":")),
                active=True,
                created_at_ms=created,
                updated_at_ms=created,
            )
            session.add(rule)
            session.flush()
            session.add(AuditLog(
                operator_type="web-admin", operator_id=operator_id, operation="gradient-rule.create",
                target=str(rule.id), after_json=json.dumps({"name": rule.name, "tiers": canonical}),
                reason=reason.strip(), created_at_ms=created,
            ))
            return rule.id

    def update_gradient_rule(self, rule_id: int, name: str, description: str | None,
                             tiers: list[dict[str, Any]], reason: str,
                             operator_id: str = "admin-token") -> None:
        if not reason.strip():
            raise BillingError("修改梯度规则必须填写原因")
        canonical = self._canonical_tiers(tiers)
        normalized_name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", normalized_name):
            raise BillingError("规则名称只能使用字母、数字、点、下划线或短横线")
        with self.db.session() as session:
            rule = session.get(GradientRule, rule_id)
            if rule is None or not rule.active:
                raise BillingError("梯度规则不存在")
            duplicate = session.scalar(select(GradientRule).where(GradientRule.name == normalized_name, GradientRule.id != rule_id))
            if duplicate:
                raise BillingError("梯度规则名称已存在")
            before = {"name": rule.name, "description": rule.description, "tiers": json.loads(rule.tiers_json)}
            rule.name = normalized_name
            rule.description = (description or "").strip()[:300] or None
            rule.tiers_json = json.dumps(canonical, separators=(",", ":"))
            rule.updated_at_ms = now_ms()
            cycles = list(session.scalars(select(BillingCycle).where(
                BillingCycle.gradient_rule_id == rule.id,
                BillingCycle.status != "closed",
            )))
            for cycle in cycles:
                cycle.tiers_json = rule.tiers_json
            self._invalidate_cycle_previews(session, [cycle.id for cycle in cycles])
            session.add(AuditLog(
                operator_type="web-admin", operator_id=operator_id, operation="gradient-rule.update",
                target=str(rule.id), before_json=json.dumps(before),
                after_json=json.dumps({"name": rule.name, "description": rule.description, "tiers": canonical}),
                reason=reason.strip(), created_at_ms=now_ms(),
            ))

    def delete_gradient_rule(self, rule_id: int, reason: str, operator_id: str = "admin-token") -> None:
        if not reason.strip():
            raise BillingError("删除梯度规则必须填写原因")
        with self.db.session() as session:
            rule = session.get(GradientRule, rule_id)
            if rule is None or not rule.active:
                raise BillingError("梯度规则不存在")
            if session.scalar(select(func.count()).select_from(BillingCycle).where(
                BillingCycle.gradient_rule_id == rule.id,
                BillingCycle.status != "closed",
            )):
                raise BillingError("梯度规则仍被未关闭账期使用")
            rule.active = False
            rule.updated_at_ms = now_ms()
            session.add(AuditLog(
                operator_type="web-admin", operator_id=operator_id, operation="gradient-rule.delete",
                target=str(rule.id), before_json=json.dumps({"name": rule.name}),
                reason=reason.strip(), created_at_ms=now_ms(),
            ))

    def configure_cycle(self, name: str, gradient_rule_id: int, pool_costs: list[dict[str, Any]],
                        reason: str, operator_id: str = "admin-token") -> None:
        if not reason.strip():
            raise BillingError("修改账期配置必须填写原因")
        normalized_costs: dict[int, int] = {}
        for item in pool_costs:
            pool_id = int(item.get("pool_id") or 0)
            cents = int(item.get("fixed_cost_cents") or 0)
            if pool_id <= 0 or cents < 0:
                raise BillingError("资源池成本配置无效")
            normalized_costs[pool_id] = cents
        with self.db.session() as session:
            cycle = session.scalar(select(BillingCycle).where(BillingCycle.name == name))
            rule = session.get(GradientRule, gradient_rule_id)
            if cycle is None or cycle.status == "closed":
                raise BillingError("账期不存在或已经关闭")
            if rule is None or not rule.active:
                raise BillingError("梯度规则不存在或已停用")
            pools = {pool.id for pool in session.scalars(select(ResourcePool).where(ResourcePool.active.is_(True)))}
            if set(normalized_costs) - pools:
                raise BillingError("资源池不存在或已停用")
            before_costs = {row.pool_id: row.fixed_cost_cents for row in session.scalars(
                select(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id)
            )}
            before = {"gradient_rule_id": cycle.gradient_rule_id, "pool_costs": before_costs}
            cycle.gradient_rule_id = rule.id
            cycle.tiers_json = rule.tiers_json
            session.execute(delete(CyclePoolCost).where(CyclePoolCost.cycle_id == cycle.id))
            for pool_id, cents in normalized_costs.items():
                session.add(CyclePoolCost(cycle_id=cycle.id, pool_id=pool_id, fixed_cost_cents=cents))
            self._invalidate_cycle_previews(session, [cycle.id])
            session.add(AuditLog(
                operator_type="web-admin", operator_id=operator_id, operation="cycle.configure",
                target=cycle.name, before_json=json.dumps(before),
                after_json=json.dumps({"gradient_rule_id": rule.id, "pool_costs": normalized_costs}),
                reason=reason.strip(), created_at_ms=now_ms(),
            ))

    def create_cycle(self, name: str, start: str, end: str, fixed_cost_cents: int, waiver: str | None = None,
                     gradient_rule_id: int | None = None, pool_costs: list[dict[str, Any]] | None = None,
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
            gradient = session.get(GradientRule, gradient_rule_id) if gradient_rule_id else session.scalar(
                select(GradientRule).where(GradientRule.active.is_(True)).order_by(GradientRule.id)
            )
            if gradient is None or not gradient.active:
                raise BillingError("active gradient rule is missing")
            cycle = BillingCycle(name=name, start_at_ms=start_ms, end_at_ms=end_ms, timezone=self.settings.timezone, status="open",
                                 pricing_version_id=version_id, gradient_rule_id=gradient.id,
                                 tiers_json=gradient.tiers_json, data_quality_waiver=waiver, created_at_ms=now_ms())
            session.add(cycle)
            session.flush()
            if pool_costs is None:
                pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
                if pool is None:
                    raise BillingError("default pool is missing")
                normalized_costs = {pool.id: fixed_cost_cents}
            else:
                normalized_costs = {int(item["pool_id"]): int(item["fixed_cost_cents"]) for item in pool_costs}
            for pool_id, cents in normalized_costs.items():
                if cents < 0 or session.get(ResourcePool, pool_id) is None:
                    raise BillingError("invalid resource pool cost")
                session.add(CyclePoolCost(cycle_id=cycle.id, pool_id=pool_id, fixed_cost_cents=cents))
            if operator_type and operator_id:
                session.add(AuditLog(operator_type=operator_type, operator_id=operator_id, operation="cycle.create", target=name,
                                     after_json=json.dumps({"start_at_ms": start_ms, "end_at_ms": end_ms,
                                                            "pool_costs": normalized_costs,
                                                            "gradient_rule_id": gradient.id,
                                                            "waiver": waiver}), created_at_ms=now_ms()))
