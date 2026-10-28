from __future__ import annotations

import sqlite3
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from psycopg.types.numeric import NumericLoader
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .integrity import install_integrity_objects
from .models import Base


def now_ms() -> int:
    return int(time.time() * 1000)


class _SQLiteCompatibleNumericLoader(NumericLoader):
    """PostgreSQL returns SUM(bigint) and AVG() as NUMERIC; SQLite returns int/float, which the services expect."""

    def load(self, data: Any) -> int | float:
        value = super().load(data)
        if value == value.to_integral_value():
            return int(value)
        return float(value)


def database_url(target: Path | str) -> str:
    """A Path is a SQLite file; a string is a SQLAlchemy URL such as postgresql+psycopg://..."""
    if isinstance(target, Path):
        return f"sqlite:///{target}"
    return target


class Database:
    # Overflow covers the web threadpool (anyio defaults to 40 threads) plus asyncio.to_thread workers;
    # SQLite connections are cheap and WAL readers run concurrently while writers wait on the busy timeout.
    def __init__(self, target: Path | str, *, pool_size: int = 10, max_overflow: int = 50, pool_timeout: float = 30) -> None:
        self.url = database_url(target)
        self.dialect = make_url(self.url).get_backend_name()
        if self.dialect == "sqlite":
            path = Path(make_url(self.url).database or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                self.url,
                # sqlite3's timeout is the busy timeout (15 s).
                connect_args={"check_same_thread": False, "timeout": 15},
                pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout,
            )

            @event.listens_for(self.engine, "connect")
            def configure(connection: sqlite3.Connection, _: object) -> None:
                cursor = connection.cursor()
                cursor.execute("pragma journal_mode=wal")
                cursor.execute("pragma foreign_keys=on")
                cursor.execute("pragma synchronous=normal")
                # Large GROUP BY / ORDER BY intermediates can spill to disk.
                cursor.execute("pragma temp_store=file")
                cursor.close()
        else:
            # Server databases enforce foreign keys natively; pre-ping drops connections closed by the server.
            self.engine = create_engine(
                self.url, pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout, pool_pre_ping=True,
            )
            if self.dialect == "postgresql":
                @event.listens_for(self.engine, "connect")
                def load_numeric_like_sqlite(connection: Any, _: object) -> None:
                    connection.adapters.register_loader("numeric", _SQLiteCompatibleNumericLoader)

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            install_integrity_objects(connection)

    def session(self) -> AbstractContextManager[Session]:
        # Commits on success, rolls back on error, always closes.
        return self.sessions.begin()
