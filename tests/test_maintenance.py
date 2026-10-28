from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from cpa_billing.database import Database
from cpa_billing.maintenance import backup_database, copy_database
from cpa_billing.security import cpamp_key_hash
from test_services import create_owner, insert_event


def billed_service(service, settings):
    create_owner(service, "key", 2, 0)
    insert_event(settings, cpamp_key_hash("key"), 1000)
    service.sync_cpamp(); service.rate_events()
    service.create_cycle("cycle", "1970-01-01T08:00", "1970-01-02T08:00", 10000)
    service.close_cycle("cycle", 1, False)
    with service.db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('head-under-test')"))
    return service


def test_copy_database_preserves_closed_bills_and_reinstalls_triggers(service, settings, tmp_path) -> None:
    billed_service(service, settings)
    target = Database(tmp_path / "copy.sqlite")
    copied = copy_database(service.db, target, batch_size=1)
    assert copied["statements"] == 1 and copied["statement_lines"] == 1 and copied["raw_usage_events"] == 1
    with target.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "head-under-test"
        bill = connection.execute(text("SELECT amount_yuan, final FROM v_user_cycle_bills")).one()
        assert (float(bill[0]), bool(bill[1])) == (100.0, True)
    with pytest.raises(Exception, match="final statement is frozen"):
        with target.engine.begin() as connection:
            connection.execute(text("UPDATE statements SET amount_cents = 0"))
    with pytest.raises(RuntimeError, match="must not contain any tables"):
        copy_database(service.db, target)


def test_copy_database_requires_a_migrated_source(service, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        copy_database(service.db, Database(tmp_path / "copy.sqlite"))


def test_sqlite_backup_is_a_consistent_snapshot(service, settings, tmp_path) -> None:
    if service.db.dialect != "sqlite":
        pytest.skip("PostgreSQL backups use pg_dump")
    billed_service(service, settings)
    snapshot = backup_database(service.db, tmp_path / "backup" / "billing.sqlite")
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert connection.execute("select count(*) from statements where final").fetchone()[0] == 1
