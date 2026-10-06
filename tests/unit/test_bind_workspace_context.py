"""`bind_workspace_context` must not touch the pool, and must set the GUC first
in every transaction (incident 2026-10-06: api thread-pool / connection-pool
deadlock under the SaaS sync burst).

`app.deps.get_current_principal` used to call `set_workspace_context` before
yielding: that statement checks a pooled connection out in one worker thread,
and the handler then needs a second worker thread. With more concurrent
requests than connections, the thread pool filled with requests blocked in
`pool.connect()` while the connection holders starved for a thread -- every
request waited the full 15 s `pool_timeout`, Postgres idle. The dependency
now binds the context to the session's `after_begin` event, so the
`set_config` runs on the handler's thread, inside the handler's transaction,
and nothing is checked out at the yield.

SQLite stands in for Postgres: `set_config` is registered as a SQL function on
every connection so the exact statement the production code emits parses and
runs; `before_cursor_execute` records the statement order.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app_shared.database import bind_workspace_context

WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _register_set_config(dbapi_connection, _record) -> None:  # noqa: ANN001
        # Postgres' set_config(name, value, is_local) returns the value set.
        dbapi_connection.create_function("set_config", 3, lambda _n, value, _l: value)

    statements: list[str] = []

    @event.listens_for(eng, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _params, _ctx, _many) -> None:  # noqa: ANN001
        statements.append(statement.strip())

    eng.recorded = statements  # type: ignore[attr-defined]
    yield eng
    eng.dispose()


def _session(engine) -> Session:  # noqa: ANN001
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_bind_does_not_check_out_a_connection(engine) -> None:  # noqa: ANN001
    session = _session(engine)
    bind_workspace_context(session, WORKSPACE_ID)
    assert engine.pool.checkedout() == 0, "binding the context must not touch the pool"
    assert engine.recorded == []
    session.close()


def test_set_config_runs_first_in_the_handlers_transaction(engine) -> None:  # noqa: ANN001
    session = _session(engine)
    bind_workspace_context(session, WORKSPACE_ID)

    value = session.execute(text("SELECT 42")).scalar_one()

    assert value == 42
    assert engine.recorded[0].startswith("SELECT set_config('app.workspace_id'")
    assert engine.recorded[1] == "SELECT 42"
    # The GUC statement is bound, never interpolated.
    assert WORKSPACE_ID not in engine.recorded[0]
    session.close()


def test_context_is_reapplied_after_commit(engine) -> None:  # noqa: ANN001
    session = _session(engine)
    bind_workspace_context(session, WORKSPACE_ID)

    session.execute(text("SELECT 1"))
    session.commit()
    session.execute(text("SELECT 2"))

    set_configs = [i for i, s in enumerate(engine.recorded) if s.startswith("SELECT set_config")]
    assert len(set_configs) == 2, engine.recorded
    assert engine.recorded.index("SELECT 2") > set_configs[1]
    session.close()


def test_refuses_a_session_already_in_a_transaction(engine) -> None:  # noqa: ANN001
    session = _session(engine)
    session.execute(text("SELECT 1"))
    with pytest.raises(RuntimeError, match="before the session begins a transaction"):
        bind_workspace_context(session, WORKSPACE_ID)
    session.close()


def test_get_current_principal_yields_without_a_checked_out_connection(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """End to end through the real dependency: at the yield, the pool is untouched."""
    import app.deps as deps

    @contextmanager
    def _real_sqlite_session():
        session = _session(engine)
        try:
            yield session
        finally:
            session.close()

    workspace_id = uuid.uuid4()
    principal = deps.Principal(
        kind="api_key",
        id=uuid.uuid4(),
        role=None,
        scopes=["products:read"],
        workspace_id=workspace_id,
    )
    monkeypatch.setattr(deps, "get_session", _real_sqlite_session)
    monkeypatch.setattr(deps, "_authenticate_api_key", lambda credential: principal)

    gen = deps.get_current_principal(
        authorization=f"Bearer {deps.API_KEY_PREFIX}synthetic", x_workspace_id=None
    )
    session, yielded = next(gen)

    assert yielded.workspace_id == workspace_id
    assert engine.pool.checkedout() == 0, "the dependency held a pooled connection across its yield"

    # The handler's first statement checks a connection out and sets the context first.
    session.execute(text("SELECT 1"))
    assert engine.recorded[0].startswith("SELECT set_config('app.workspace_id'")
    with pytest.raises(StopIteration):
        next(gen)
    assert engine.pool.checkedout() == 0
