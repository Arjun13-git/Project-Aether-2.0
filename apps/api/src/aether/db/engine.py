"""Database engine (SQLAlchemy Core over psycopg 3). Queries are explicit SQL in repositories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, text

from aether.settings import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().require_db(), pool_pre_ping=True, future=True)


@contextmanager
def transaction(engine: Engine | None = None) -> Iterator[Connection]:
    with (engine or get_engine()).begin() as conn:
        yield conn


def allow_project_delete(conn: Connection) -> None:
    """Let the ownership cascade delete immutable rows inside this transaction only."""
    conn.execute(text("SET LOCAL aether.project_delete = 'on'"))
