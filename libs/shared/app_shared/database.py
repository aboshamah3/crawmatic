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

F15 (EPA core-production-readiness, 2026-09-07): every engine built here
now sets a Postgres ``statement_timeout`` (via a psycopg ``-c`` connect
option, so it applies to every session on the connection regardless of
pooling) and a SQLAlchemy ``pool_timeout`` (how long a caller waits for
a pooled connection before giving up), both deadline-bearing knobs this
module never had before -- an unbounded query or an exhausted pool used
to be able to hang a request (and, transitively, whatever event-loop or
thread served it) indefinitely.

Both are read from ``os.environ`` (see the CONFIG NOTE below) with
role-appropriate defaults: this module is shared by every service, and
the plan's own numbers differ by role -- the API wants a tight
15,000 ms ceiling since it serves interactive requests, while a worker
running a long sweep needs 120,000 ms or more. The *default* here is
the API's tighter number, since a request that hangs the API is the
more visible failure; a worker process sets
``DB_STATEMENT_TIMEOUT_MS=120000`` (and ``DB_POOL_ACQUIRE_TIMEOUT_SECONDS``
to match) in its own environment to get the looser ceiling. A specific
maintenance/sweep task that needs to exceed even that can use
:func:`override_statement_timeout` to raise the limit for just its own
transaction rather than changing the process-wide default.

CONFIG NOTE: ``DB_STATEMENT_TIMEOUT_MS`` and
``DB_POOL_ACQUIRE_TIMEOUT_SECONDS`` are read from ``os.environ`` rather
than ``app_shared.config.Settings`` -- B7 ran in parallel with another
worker holding ``config.py`` for the run this landed in. They belong
there as typed settings; this is a placeholder until that consolidation
lands (see ``reports/B7.md``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app_shared.config import get_settings

#: See the CONFIG NOTE above: placeholder env-var reads until these are
#: consolidated into `app_shared.config.Settings`. Default matches the
#: API's tighter ceiling (15s/15000ms); a worker process overrides both
#: via its own environment for the plan's 120,000 ms figure.
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "15000"))
DB_POOL_ACQUIRE_TIMEOUT_SECONDS = float(
    os.environ.get("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "15")
)

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None
_auth_engine: Engine | None = None
_auth_sessionmaker: sessionmaker[Session] | None = None
_system_engine: Engine | None = None
_system_sessionmaker: sessionmaker[Session] | None = None


def _reset_workspace_context(dbapi_connection, connection_record, connection_proxy) -> None:
    """Clear ``app.workspace_id`` as a connection leaves the pool for a caller.

    READY-007 / P0.5. Every workspace context this codebase sets is
    transaction-local (:func:`set_workspace_context` passes
    ``is_local=true``), and a ``SET LOCAL`` cannot outlive its
    transaction. That is the first line of defence and it holds today.

    This is the second line, and it exists because the failure it
    prevents is silent and total. A plain ``SET app.workspace_id`` —
    one stray statement, in a migration helper, a debugging session, a
    library — is **session**-scoped, and a session-scoped GUC survives
    ``ROLLBACK``, which is exactly what SQLAlchemy's default
    ``reset_on_return`` issues at check-in. The connection then goes
    back to the pool still carrying one tenant's context and is handed
    to the next request, which may be another tenant's. RLS would be
    working perfectly and still return the wrong workspace's rows,
    because the predicate is only ever as good as the GUC it reads.

    ``RESET`` is issued on **checkout** rather than check-in on purpose:
    check-in can run against a connection in a failed transaction (where
    every statement errors), while checkout is guaranteed to hand back a
    usable connection — and a reset that silently failed to run would be
    worse than none, since it would look like protection.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("RESET app.workspace_id")
    finally:
        cursor.close()


def install_workspace_context_reset(engine: Engine) -> Engine:
    """Attach :func:`_reset_workspace_context` to ``engine``'s pool, once."""
    if not event.contains(engine, "checkout", _reset_workspace_context):
        event.listen(engine, "checkout", _reset_workspace_context)
    return engine


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
            pool_timeout=DB_POOL_ACQUIRE_TIMEOUT_SECONDS,
            connect_args={
                # PgBouncer transaction pooling: disable psycopg's
                # server-side prepared-statement cache (see module
                # docstring).
                "prepare_threshold": None,
                # F15: bound every statement on this connection so a
                # runaway query cannot hang a caller indefinitely.
                "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
            },
        )
        # READY-007: a pooled connection must never carry one tenant's
        # workspace context into the next tenant's checkout. See
        # `_reset_workspace_context`.
        install_workspace_context_reset(_engine)
        # Audit C3: this is the ONE ordinary, RLS-confined connection.
        # Assert once, here, at engine construction (i.e. once per
        # process on first use — never per request) that the role behind
        # it cannot step over FORCE ROW LEVEL SECURITY. No-op outside
        # production unless RLS_ROLE_ASSERTION=on; see
        # app_shared.db.rls_guard. Deliberately NOT applied to the auth
        # or system engines below, which are BYPASSRLS by design.
        from app_shared.db.rls_guard import enforce_rls_role_on_startup

        try:
            enforce_rls_role_on_startup(_engine)
        except BaseException:
            _engine.dispose()
            _engine = None
            raise
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


@contextmanager
def override_statement_timeout(session: Session, timeout_ms: int) -> Iterator[None]:
    """Raise (or lower) ``statement_timeout`` for the rest of this transaction only.

    F15's per-role process defaults (see module docstring) are a floor
    for interactive traffic, not a ceiling every query must fit under --
    a maintenance or sweep task that legitimately needs more time than
    its process default should reach for this rather than raising the
    process-wide ``DB_STATEMENT_TIMEOUT_MS``, which would silently loosen
    the deadline for every *other* query on the same connection/process.

    Uses ``SET LOCAL`` (bound parameter, no string interpolation), the
    same transaction-scoped mechanism as :func:`set_workspace_context` --
    it never outlives the current transaction and needs no explicit
    reset. Must be called on a session already inside a transaction
    (i.e. after at least one prior statement, or right before one);
    ``SET LOCAL`` outside a transaction block is a silent no-op in
    Postgres.
    """
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout_ms, true)"),
        {"timeout_ms": str(int(timeout_ms))},
    )
    yield


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

    Scope: three sanctioned users.

    1. Credential resolution (the original, narrower purpose): finding a
       row by unique email / key prefix, pre-auth. Once a principal is
       resolved this way, all further workspace-owned access goes
       through the ordinary :func:`get_session` engine with
       :func:`set_workspace_context` + RLS — never through this
       BYPASSRLS session.
    1b. ``refresh_tokens`` in its entirety (READY-007 / P0.5, EPA B8b;
       the table gained transitive RLS through ``users.user_id`` at
       alembic head ``b6d94c2f1a70``). This is the same carve-out as
       (1), not a widening of it: the rotation and the revocation are
       keyed by an unforgeable ``token_hash`` and resolve the principal,
       so no ``app.workspace_id`` exists yet to scope them; and the
       issue INSERT is unscopeable in principle because a
       ``SUPER_ADMIN``'s ``workspace_id`` is ``NULL`` and satisfies no
       context. See ``apps/api/app/routers/auth.py``.
    2. The SaaS admin control plane (`apps/api/app/routers/admin.py`,
       PLAN §7.1–§7.2, guarded by `app.service_auth.require_service_token`
       rather than the workspace seam): provisioning, archiving, and the
       usage export are all *legitimately* cross-workspace by
       construction (provisioning creates a workspace before any
       `app.workspace_id` GUC could apply to it; the usage export
       aggregates over every workspace in one statement), so this
       surface performs INSERT/UPDATE/aggregate reads on this same
       BYPASSRLS session rather than treating it as select-only. Every
       statement the admin router issues here is deliberately unscoped
       and carries its own `# noqa: workspace-scope` marker.

    No other caller may use this session — every other tenant-owned
    read/write still goes through :func:`get_session` +
    :func:`set_workspace_context` + RLS.
    """
    session_factory = get_auth_sessionmaker()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_system_engine() -> Engine:
    """Return the per-process scheduler-system-role engine, creating it on first use.

    Bound to ``Settings.SYSTEM_DATABASE_URL``, falling back to
    ``Settings.AUTH_DATABASE_URL`` when unset (research R2, SPEC-13). A
    dedicated BYPASSRLS role used ONLY by the scheduler's due-rule claim
    (:func:`get_system_session`) — the claim is inherently cross-tenant
    (one query must see due ``refresh_rules`` rows across every
    workspace), which ``FORCE ROW LEVEL SECURITY`` makes impossible for
    the ordinary pooler role with no ``app.workspace_id`` set. Raises
    :class:`RuntimeError` if neither ``SYSTEM_DATABASE_URL`` nor
    ``AUTH_DATABASE_URL`` is configured; see :func:`get_system_session`
    for the fail-fast rationale (mirrors :func:`get_auth_engine`).
    """
    global _system_engine
    if _system_engine is None:
        settings = get_settings()
        database_url = settings.SYSTEM_DATABASE_URL or settings.AUTH_DATABASE_URL
        if not database_url:
            raise RuntimeError(
                "SYSTEM_DATABASE_URL (or its AUTH_DATABASE_URL fallback) is "
                "required for the scheduler's cross-tenant refresh-rule claim; "
                "the due-rule scan returns 0 rows without a BYPASSRLS role "
                "under forced RLS."
            )
        _system_engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={
                # Same PgBouncer transaction-pooling constraint as the
                # main engine (see module docstring): disable server-side
                # prepared statements.
                "prepare_threshold": None,
            },
        )
    return _system_engine


def get_system_sessionmaker() -> sessionmaker[Session]:
    """Return the per-process system-role ``sessionmaker``, creating it on first use."""
    global _system_sessionmaker
    if _system_sessionmaker is None:
        _system_sessionmaker = sessionmaker(bind=get_system_engine(), expire_on_commit=False)
    return _system_sessionmaker


@contextmanager
def get_system_session() -> Iterator[Session]:
    """Yield a BYPASSRLS :class:`Session` for the scheduler's due-rule claim ONLY.

    Bound to ``Settings.SYSTEM_DATABASE_URL`` (falling back to
    ``Settings.AUTH_DATABASE_URL``, research R2). This is the **one**
    documented deviation for SPEC-13 (plan.md Complexity Tracking): the
    refresh-rule claim (``SELECT ... FOR UPDATE SKIP LOCKED``) is
    structurally cross-tenant, so it must bypass RLS to see due rows
    across every workspace. Workspace isolation is **not** relaxed by
    this seam — every read/write the scheduler performs for a claimed
    rule still goes through app-level scoping (``scoped_select(...,
    rule.workspace_id)`` / explicit ``workspace_id=`` on inserted
    job/target rows, exactly as the SPEC-08 job service already does).

    Scope: used ONLY by the scheduler's refresh pass
    (``apps/scheduler/app/scheduler/refresh.py``). The API CRUD path
    (``/v1/refresh-rules``) always keeps the ordinary RLS-enforced
    request session (:func:`get_session` + :func:`set_workspace_context`)
    — it must never use this BYPASSRLS session.
    """
    session_factory = get_system_sessionmaker()
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
    global _system_engine, _system_sessionmaker
    from app_shared.db.rls_guard import reset_rls_guard_cache

    reset_rls_guard_cache()
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
    if _auth_engine is not None:
        _auth_engine.dispose()
    _auth_engine = None
    _auth_sessionmaker = None
    if _system_engine is not None:
        _system_engine.dispose()
    _system_engine = None
    _system_sessionmaker = None
