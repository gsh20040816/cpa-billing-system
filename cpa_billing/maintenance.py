"""Backup and cross-database copy for the billing database."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import insert, inspect, select, text
from sqlalchemy.engine import make_url

from .database import Database
from .integrity import install_integrity_objects
from .models import Base


def backup_database(db: Database, destination: Path) -> Path:
    """Write a consistent snapshot while services keep running: SQLite online backup, or pg_dump custom format."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if db.dialect == "sqlite":
        source = sqlite3.connect(make_url(db.url).database or "")
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination
    if db.dialect == "postgresql":
        if shutil.which("pg_dump") is None:
            raise RuntimeError("pg_dump is required to back up PostgreSQL")
        url = make_url(db.url).set(drivername="postgresql")
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", f"--file={destination}",
             f"--dbname={url.render_as_string(hide_password=False)}"],
            check=True,
        )
        return destination
    raise RuntimeError(f"backup is not supported for {db.dialect}")


def copy_database(source: Database, target: Database, batch_size: int = 5000) -> dict[str, int]:
    """Copy every billing table from a database migrated to head into a target database with no tables.

    The target schema is created from the models (the head revision) and stamped with the source's Alembic
    revision. Integrity triggers are installed after the data, because closed cycles would otherwise reject
    their own historical rows; CHECK constraints and foreign keys apply to every copied row.
    """
    with source.engine.connect() as origin:
        if not inspect(origin).has_table("alembic_version"):
            raise RuntimeError("source database is not managed by Alembic; run `alembic upgrade head` first")
        revision = origin.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    with target.engine.connect() as destination:
        if inspect(destination).get_table_names():
            raise RuntimeError("target database must not contain any tables")
    copied: dict[str, int] = {}
    with target.engine.begin() as destination:
        Base.metadata.create_all(destination)
        destination.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"))
        destination.execute(text("INSERT INTO alembic_version (version_num) VALUES (:revision)"), {"revision": revision})
        with source.engine.connect() as origin:
            for table in Base.metadata.sorted_tables:
                order = list(table.primary_key.columns)
                result = origin.execution_options(stream_results=True).execute(select(table).order_by(*order))
                count = 0
                while batch := result.mappings().fetchmany(batch_size):
                    destination.execute(insert(table), [dict(row) for row in batch])
                    count += len(batch)
                copied[table.name] = count
        if target.dialect == "postgresql":
            _reset_sequences(destination)
        install_integrity_objects(destination)
    return copied


def _reset_sequences(connection: Any) -> None:
    # Explicit ids were copied, so identity sequences must continue after the largest one.
    for table in Base.metadata.sorted_tables:
        if "id" in table.c and table.c.id.autoincrement is not False and table.c.id.primary_key:
            sequence = connection.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table.name}).scalar()
            if sequence:
                connection.execute(
                    text(f"SELECT setval(:sequence, COALESCE((SELECT max(id) FROM {table.name}), 0) + 1, false)"),
                    {"sequence": sequence},
                )
