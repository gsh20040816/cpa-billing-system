from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .database import now_ms
from .models import (
    APIKey,
    AllowedChat,
    BillingCycle,
    CyclePoolCost,
    GradientRule,
    GroupMembership,
    KeyOwnershipPeriod,
    ResourcePool,
    TelegramUser,
)
from .security import login_fingerprint, mask_api_key, mask_hash
from .services import BillingService


def _milliseconds(value: int | float | None) -> int | None:
    return None if value is None else int(value) * 1000


def migrate_legacy_bot(service: BillingService, legacy_path: Path, dry_run: bool = False) -> dict[str, int]:
    if not legacy_path.exists():
        raise RuntimeError(f"legacy bot database not found: {legacy_path}")
    service.bootstrap()
    source = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts = {"users": 0, "keys": 0, "ownerships": 0, "memberships": 0, "cycles": 0}
    raw_by_hash = {str(row["api_key_hash"]): str(row["api_key"]) for row in source.execute("select api_key_hash,api_key from registrations")}
    status_by_hash = {str(row["api_key_hash"]): ("revoked" if row["revoked_at"] else "active", _milliseconds(row["revoked_at"]))
                      for row in source.execute("select api_key_hash,revoked_at from registrations")}
    labels = {str(row["api_key_hash"]): str(row["label"]) for row in source.execute("select api_key_hash,label from api_key_labels")}
    if dry_run:
        for table, key in [("users", "users"), ("api_key_ownerships", "ownerships"), ("group_memberships", "memberships"), ("billing_cycles", "cycles")]:
            counts[key] = int(source.execute(f"select count(*) from {table}").fetchone()[0])
        counts["keys"] = counts["ownerships"]
        return counts
    with service.db.session() as session:
        manual = {int(row[0]) for row in source.execute("select telegram_user_id from manual_allowed_users")}
        registration_times: dict[int, int] = {}
        for row in source.execute("select telegram_user_id,min(created_at) from registrations group by telegram_user_id"):
            registration_times[int(row[0])] = int(row[1]) * 1000
        for row in source.execute("select * from users"):
            user_id = int(row["telegram_user_id"])
            user = session.get(TelegramUser, user_id)
            if user is None:
                user = TelegramUser(telegram_user_id=user_id, last_seen_at_ms=int(row["last_seen_at"]) * 1000)
                session.add(user)
                counts["users"] += 1
            user.username, user.first_name, user.last_name = row["username"], row["first_name"], row["last_name"]
            user.registered_at_ms = registration_times.get(user_id)
            user.manual_allowed = user_id in manual
            user.is_admin = user_id in service.settings.admin_user_ids
        session.flush()
        for row in source.execute("select * from group_memberships"):
            membership = session.get(GroupMembership, (int(row["telegram_user_id"]), int(row["group_chat_id"])))
            if membership is None:
                session.add(GroupMembership(telegram_user_id=int(row["telegram_user_id"]), group_chat_id=int(row["group_chat_id"]),
                                            status=str(row["status"]), legal=bool(row["legal"]), updated_at_ms=int(row["updated_at"]) * 1000))
                counts["memberships"] += 1
        for row in source.execute("select * from allowed_chats"):
            if session.get(AllowedChat, int(row["chat_id"])) is None:
                session.add(AllowedChat(chat_id=int(row["chat_id"]), note=row["note"], updated_at_ms=int(row["updated_at"]) * 1000))
        for row in source.execute("select * from api_key_ownerships order by first_seen_at"):
            key_hash = str(row["api_key_hash"])
            key = session.scalar(select(APIKey).where(APIKey.cpamp_hash == key_hash))
            raw = raw_by_hash.get(key_hash)
            status, revoked_at = status_by_hash.get(key_hash, ("retired" if row["retired_at"] else "active", _milliseconds(row["retired_at"])))
            if key is None:
                key = APIKey(cpamp_hash=key_hash, login_fingerprint=login_fingerprint(raw, service.settings.key_pepper) if raw else None,
                             masked_value=mask_api_key(raw) if raw else mask_hash(key_hash), display_name=labels.get(key_hash), status=status,
                             current_owner_id=None if row["retired_at"] else int(row["telegram_user_id"]),
                             created_at_ms=int(row["first_seen_at"]) * 1000, revoked_at_ms=revoked_at)
                session.add(key)
                session.flush()
                counts["keys"] += 1
            exists = session.scalar(select(KeyOwnershipPeriod).where(KeyOwnershipPeriod.api_key_id == key.id,
                                                                     KeyOwnershipPeriod.valid_from_ms == int(row["first_seen_at"]) * 1000))
            if exists is None:
                session.add(KeyOwnershipPeriod(api_key_id=key.id, telegram_user_id=int(row["telegram_user_id"]),
                                               valid_from_ms=int(row["first_seen_at"]) * 1000, valid_to_ms=_milliseconds(row["retired_at"]),
                                               source="legacy-migration", reason="Imported from cpa-tg-bot", created_at_ms=now_ms()))
                counts["ownerships"] += 1
        version_id = service._active_pricing_id(session)
        pool = session.scalar(select(ResourcePool).where(ResourcePool.name == "default-cpa"))
        default_gradient = session.scalar(select(GradientRule).where(GradientRule.name == "default-gradient"))
        if default_gradient is None:
            raise RuntimeError("default gradient rule is missing")
        default_tiers = json.loads(default_gradient.tiers_json)
        for row in source.execute("select * from billing_cycles order by start_at"):
            if session.scalar(select(BillingCycle).where(BillingCycle.name == row["name"])):
                continue
            zone = ZoneInfo(service.settings.timezone)
            start, end = datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])
            if start.tzinfo is None: start = start.replace(tzinfo=zone)
            if end.tzinfo is None: end = end.replace(tzinfo=zone)
            tiers_json = str(row["tiers_json"])
            try:
                tiers = json.loads(tiers_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"legacy cycle {row['name']} has invalid tiers JSON") from exc
            gradient = default_gradient
            if tiers != default_tiers:
                rule_name = f"legacy-{row['name']}"[:80]
                gradient = session.scalar(select(GradientRule).where(GradientRule.name == rule_name))
                if gradient is None:
                    gradient = GradientRule(
                        name=rule_name,
                        description="Imported from legacy cpa-tg-bot billing cycle",
                        tiers_json=tiers_json,
                        active=True,
                        created_at_ms=now_ms(),
                        updated_at_ms=now_ms(),
                    )
                    session.add(gradient)
                    session.flush()
            cycle = BillingCycle(name=str(row["name"]), start_at_ms=int(start.timestamp() * 1000), end_at_ms=int(end.timestamp() * 1000),
                                 timezone=service.settings.timezone, status="open", pricing_version_id=version_id,
                                 gradient_rule_id=gradient.id, tiers_json=tiers_json,
                                 data_quality_waiver=("本周期曾启用 codexcomp；CPAMP 未保存 metadata.proxy_billed_usage，部分多轮请求的真实上游用量可能被低估。"
                                                      if str(row["name"]) == "cycle0" else None), created_at_ms=int(row["created_at"]) * 1000)
            session.add(cycle)
            session.flush()
            session.add(CyclePoolCost(cycle_id=cycle.id, pool_id=pool.id, fixed_cost_cents=int(round(float(row["fixed_cost"]) * 100))))
            counts["cycles"] += 1
    source.close()
    return counts
