"""Lazy, per-process SQLAlchemy engine + session helpers.

All application services reach Postgres only through PgBouncer in
**transaction pooling** mode (never ``postgres:5432`` directly,
FR-011). Under transaction pooling a connection is handed back to the
pool between transactions, so this module is written with two
constraints in mind:

* Server-side prepared statements must be disabled — they cannot
  survive across pooled connections. The psycopg driver is configured
  with ``prepare_threshold=None`` to disable its automatic
  prepared-statement cache.
* Session-level state (``SET``, advisory locks, temp tables, ...) must
  not be relied on outside of ``SET LOCAL`` / ``pg_advisory_xact_lock``
  scoped to a single transaction, since a session's underlying server
  connection can change between transactions.

The engine + sessionmaker are created lazily on first use and cached
as per-process singletons — **never** at import time (would defeat
fail-fast config validation and break fork-safety) and **never**
per-request (would leak pooled connections). ``dispose_engine()`` is
called from Celery's ``worker_process_init`` hook (Phase 3) so each
forked worker process rebuilds its own engine/pool instead of
inheriting a live connection across ``fork()``.

This module defines no ORM models and runs no queries — it is
connectivity plumbing only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app_shared.config import get_settings

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the per-process SQLAlchemy engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
            connect_args={
                # PgBouncer transaction pooling: disable psycopg's
                # server-side prepared-statement cache (see module
                # docstring).
                "prepare_threshold": None,
            },
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return the per-process ``sessionmaker``, creating it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessionmaker


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a :class:`~sqlalchemy.orm.Session` bound to the per-process engine."""
    session_factory = get_sessionmaker()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def check_connection() -> None:
    """Verify connectivity by executing a trivial ``SELECT 1``.

    Opens a session via :func:`get_session` (the per-process, lazily
    created engine) and runs a schema-independent query. Raises on
    failure (e.g. ``sqlalchemy.exc.OperationalError`` if the database is
    unreachable); returns ``None`` on success. Per contracts/config.md
    (FR-015).
    """
    with get_session() as session:
        session.execute(text("SELECT 1"))


def dispose_engine() -> None:
    """Dispose the per-process engine and clear the cached singletons.

    Called from the Celery ``worker_process_init`` fork hook so a
    forked worker process never reuses a connection inherited from its
    parent process.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
