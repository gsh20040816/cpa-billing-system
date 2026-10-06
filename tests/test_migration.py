from __future__ import annotations

import sqlite3

from sqlalchemy import select

from cpa_billing.migrate import migrate_legacy_bot
from cpa_billing.models import APIKey, BillingCycle, KeyOwnershipPeriod, TelegramUser
from cpa_billing.security import login_fingerprint


def legacy(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
    create table users(telegram_user_id integer primary key,username text,first_name text,last_name text,last_seen_at integer);
    create table manual_allowed_users(telegram_user_id integer primary key,note text,updated_at integer);
    create table group_memberships(telegram_user_id integer,group_chat_id integer,status text,legal integer,updated_at integer,primary key(telegram_user_id,group_chat_id));
    create table allowed_chats(chat_id integer primary key,note text,updated_at integer);
    create table registrations(id integer primary key,telegram_user_id integer,api_key text,api_key_hash text,alias text,username text,first_name text,last_name text,created_at integer,updated_at integer,revoked_at integer);
    create table api_key_ownerships(api_key_hash text primary key,telegram_user_id integer,alias text,username text,first_name text,last_name text,first_seen_at integer,updated_at integer,retired_at integer);
    create table api_key_labels(api_key_hash text primary key,label text,created_at integer,updated_at integer);
    create table billing_cycles(name text primary key,start_at text,end_at text,fixed_cost real,tiers_json text,created_at integer,updated_at integer);
    """)
    raw, h = "sk-cpa-secret-value-123456", "abc123"
    db.execute("insert into users values(2,'alice','A',NULL,10)")
    db.execute("insert into registrations values(1,2,?,?, 'alice','alice','A',NULL,10,10,NULL)", (raw, h))
    db.execute("insert into api_key_ownerships values(?,2,'alice','alice','A',NULL,10,10,NULL)", (h,))
    db.execute("insert into api_key_labels values(?,'main',10,10)", (h,))
    db.execute("insert into billing_cycles values('cycle0','2026-07-03T18:00:00+08:00','2026-07-28T12:00:00+08:00',1090,?,10,10)",
               ('[{"left":0,"right":null,"multiplier":1}]',))
    db.commit(); db.close()


def test_migration_does_not_store_raw_key(service, settings, tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"; legacy(path)
    counts = migrate_legacy_bot(service, path)
    assert counts["keys"] == 1 and counts["cycles"] == 1
    with service.db.session() as session:
        key = session.scalar(select(APIKey))
        assert key.login_fingerprint == login_fingerprint("sk-cpa-secret-value-123456", settings.key_pepper)
        assert "secret-value" not in key.masked_value
        assert session.scalar(select(BillingCycle)).data_quality_waiver
        assert session.scalar(select(KeyOwnershipPeriod)).telegram_user_id == 2


def test_worker_hot_path_migration_indexes_backfills_and_clamps_tokens_idempotently():
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "migrations/versions/0016_worker_hot_path.py"
    spec = importlib.util.spec_from_file_location("worker_hot_path_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    tokens = migration.TOKEN_COLUMNS
    engine = sa.create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(sa.text(
            "CREATE TABLE raw_usage_events (id INTEGER PRIMARY KEY, source_id INTEGER, source_event_id INTEGER, "
            "reasoning_effort TEXT, request_service_tier TEXT, response_service_tier TEXT, "
            + ", ".join(f"{column} INTEGER" for column in tokens) + ")"
        ))
        db.execute(sa.text(
            f"INSERT INTO raw_usage_events (id, source_id, source_event_id, {', '.join(tokens)}) "
            f"VALUES (1, 1, 1, -5, 7, -1, 0, 3, -2, 4, 9)"
        ))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            migration.upgrade()
        row = db.execute(sa.text("SELECT * FROM raw_usage_events")).mappings().one()
        assert [row[column] for column in tokens] == [0, 7, 0, 0, 3, 0, 4, 9]
        indexes = {item["name"] for item in sa.inspect(db).get_indexes("raw_usage_events")}
        assert {f"idx_raw_events_missing_{column}" for column in migration.BACKFILL_COLUMNS} <= indexes
        plan = db.execute(sa.text(
            "EXPLAIN QUERY PLAN SELECT id FROM raw_usage_events WHERE source_id = 1 AND reasoning_effort IS NULL "
            "ORDER BY source_event_id LIMIT 10"
        )).all()
        assert "idx_raw_events_missing_reasoning_effort" in plan[0][-1]
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
        remaining = {item["name"] for item in sa.inspect(db).get_indexes("raw_usage_events")}
        assert not remaining & {f"idx_raw_events_missing_{column}" for column in migration.BACKFILL_COLUMNS}
