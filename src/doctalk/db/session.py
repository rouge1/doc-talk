"""Engine + session management.

The engine is built lazily from ``get_settings().db_url`` and cached, so tests can repoint the
truth store (e.g. at a temp SQLite file) by setting ``DOCTALK_DB_URL`` and clearing the caches
before first use. ``session_scope`` is the standard transactional boundary; the DAG opens one
scope per stage so a crash never rolls back already-committed ledger rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from doctalk.config import get_settings


@lru_cache
def get_engine() -> Engine:
    url = get_settings().db_url
    # SQLite needs check_same_thread off for the (single-threaded) test/CLI use; harmless for it
    # to be absent on MySQL.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
    )


def reset_engine() -> None:
    """Drop cached engine/sessionmaker — used by tests after changing DOCTALK_DB_URL."""
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error, always close."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
