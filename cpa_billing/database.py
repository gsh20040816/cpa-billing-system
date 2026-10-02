from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: Path, *, pool_size: int = 5, max_overflow: int = 10, pool_timeout: float = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout,
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection: sqlite3.Connection, _: object) -> None:
            cursor = connection.cursor()
            cursor.execute("pragma journal_mode=wal")
            cursor.execute("pragma foreign_keys=on")
            cursor.execute("pragma busy_timeout=15000")
            cursor.execute("pragma synchronous=normal")
            # Large GROUP BY / ORDER BY intermediates can spill to disk.
            cursor.execute("pragma temp_store=file")
            cursor.close()

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

