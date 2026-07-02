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
_auth_engine: Engine | None = None
_auth_sessionmaker: sessionmaker[Session] | None = None


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


def set_workspace_context(session: Session, workspace_id: object) -> None:
    """Set the ``app.workspace_id`` GUC for the current transaction (FR-017).

    Executes ``SELECT set_config('app.workspace_id', :wsid, true)`` with a
    bound parameter (never string-interpolated SQL) so it is safe under
    PgBouncer transaction pooling — the ``true`` third argument makes the
    setting ``LOCAL`` (transaction-scoped), matching the ``SET LOCAL``
    semantics :func:`app_shared.models.rls.emit_rls_policy`'s fail-closed
    predicate relies on. ``workspace_id`` is coerced to ``str`` (accepts a
    ``uuid.UUID`` or a plain string) since ``current_setting`` reads GUCs
    as text.

    Must be called once per request transaction, on the session that will
    perform the workspace-scoped reads/writes for that transaction — never
    on the pre-auth ``get_auth_session()`` connection (which is BYPASSRLS
    and used only for credential resolution).
    """
    session.execute(
        text("SELECT set_config('app.workspace_id', :wsid, true)"),
        {"wsid": str(workspace_id)},
    )


def get_auth_engine() -> Engine:
    """Return the per-process auth-role engine, creating it on first use.

    Bound to ``Settings.AUTH_DATABASE_URL`` — the dedicated
    ``crawmatic_auth`` BYPASSRLS role used ONLY for pre-context credential
    lookups (user-by-email at login, api-key-by-prefix at machine auth).
    Raises :class:`RuntimeError` if ``AUTH_DATABASE_URL`` is unset; see
    :func:`get_auth_session` for why this must never fall back to
    ``DATABASE_URL``.
    """
    global _auth_engine
    if _auth_engine is None:
        settings = get_settings()
        if not settings.AUTH_DATABASE_URL:
            raise RuntimeError(
                "AUTH_DATABASE_URL (crawmatic_auth BYPASSRLS role) is required "
                "for authentication; pre-auth credential lookups return 0 rows "
                "without it under forced RLS."
            )
        _auth_engine = create_engine(
            settings.AUTH_DATABASE_URL,
            pool_pre_ping=True,
            connect_args={
                # Same PgBouncer transaction-pooling constraint as the main
                # engine (see module docstring): disable server-side
                # prepared statements.
                "prepare_threshold": None,
            },
        )
    return _auth_engine


def get_auth_sessionmaker() -> sessionmaker[Session]:
    """Return the per-process auth-role ``sessionmaker``, creating it on first use."""
    global _auth_sessionmaker
    if _auth_sessionmaker is None:
        _auth_sessionmaker = sessionmaker(bind=get_auth_engine(), expire_on_commit=False)
    return _auth_sessionmaker


@contextmanager
def get_auth_session() -> Iterator[Session]:
    """Yield a BYPASSRLS :class:`Session` for pre-auth credential lookups only.

    Bound to ``Settings.AUTH_DATABASE_URL`` (the ``crawmatic_auth`` role).
    **[analyze C1] Critical fail-fast contract**: this function MUST NOT
    silently fall back to ``DATABASE_URL`` (the pooler role, which is
    NOT BYPASSRLS) when ``AUTH_DATABASE_URL`` is unset. Under
    ``FORCE ROW LEVEL SECURITY`` (set by
    :func:`app_shared.models.rls.emit_rls_policy` on ``users``/``api_keys``),
    a non-BYPASSRLS role with no workspace context set returns **zero
    rows** for the pre-auth user-by-email / api-key-by-prefix lookup —
    silently failing closed and making every login and API-key auth
    attempt appear as "wrong credentials" with no indication of the real
    (configuration) cause. Raising here instead surfaces the
    misconfiguration immediately and loudly, via :func:`get_auth_engine`.

    Scope: this session is for credential resolution ONLY (finding the
    row by unique email / key prefix). Once a principal is resolved, all
    further workspace-owned access goes through the ordinary
    :func:`get_session` engine with :func:`set_workspace_context` +
    RLS — never through this BYPASSRLS session.
    """
    session_factory = get_auth_sessionmaker()
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
    global _engine, _sessionmaker, _auth_engine, _auth_sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
    if _auth_engine is not None:
        _auth_engine.dispose()
    _auth_engine = None
    _auth_sessionmaker = None
