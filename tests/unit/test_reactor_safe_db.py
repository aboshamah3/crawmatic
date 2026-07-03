"""Reactor-safe DB seam tests (SPEC-07 US5 T043, contracts/reactor-safe-db.md).

``scrape_core.db`` is the **decided-once** reactor-safe DB seam:
``run_in_thread`` offloads a callable through
``twisted.internet.threads.deferToThread`` (the only sanctioned way a
pipeline/middleware performs a DB or other blocking call), and
``workspace_txn`` opens a workspace-scoped session (RLS active via
``set_workspace_context``) that must run **inside** that offloaded
thread, never on the reactor.

No real database and no running Twisted reactor loop are needed here:
``get_session``/``set_workspace_context``/``deferToThread`` are all
monkeypatched with pure, synchronous fakes so these tests exercise
`scrape_core.db`'s own logic (session lifecycle, commit/rollback,
offload-not-inline) deterministically.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from scrape_core import db as db_mod


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _make_fake_get_session(session: _FakeSession) -> Any:
    @contextmanager
    def _fake_get_session() -> Iterator[_FakeSession]:
        try:
            yield session
        finally:
            session.closed = True

    return _fake_get_session


# --- workspace_txn: workspace context set before yielding, commits/rolls back --


def test_workspace_txn_sets_workspace_context_before_yielding_then_commits(
    monkeypatch: Any,
) -> None:
    session = _FakeSession()
    calls: list[tuple[Any, Any]] = []

    def _fake_set_workspace_context(sess: Any, workspace_id: Any) -> None:
        calls.append((sess, workspace_id))

    monkeypatch.setattr(db_mod, "get_session", _make_fake_get_session(session))
    monkeypatch.setattr(db_mod, "set_workspace_context", _fake_set_workspace_context)

    workspace_id = uuid.uuid4()
    with db_mod.workspace_txn(workspace_id) as yielded:
        assert yielded is session
        # The RLS context is active *before* any caller-supplied work runs.
        assert calls == [(session, workspace_id)]
        assert not session.committed
        assert not session.closed

    assert session.committed
    assert not session.rolled_back
    assert session.closed


def test_workspace_txn_rolls_back_and_reraises_on_exception(monkeypatch: Any) -> None:
    session = _FakeSession()
    monkeypatch.setattr(db_mod, "get_session", _make_fake_get_session(session))
    monkeypatch.setattr(db_mod, "set_workspace_context", lambda sess, wsid: None)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with db_mod.workspace_txn(uuid.uuid4()):
            raise _Boom("transaction body failed")

    assert session.rolled_back
    assert not session.committed
    assert session.closed  # closed either way (commit or rollback)


def test_workspace_txn_passes_the_given_workspace_id_through_unmodified(
    monkeypatch: Any,
) -> None:
    session = _FakeSession()
    seen: list[Any] = []
    monkeypatch.setattr(db_mod, "get_session", _make_fake_get_session(session))
    monkeypatch.setattr(
        db_mod, "set_workspace_context", lambda sess, wsid: seen.append(wsid)
    )

    workspace_id = uuid.uuid4()
    with db_mod.workspace_txn(workspace_id):
        pass

    assert seen == [workspace_id]


# --- run_in_thread: offloads via deferToThread, never calls fn inline ---------


def test_run_in_thread_offloads_via_deferToThread_never_calls_fn_inline(
    monkeypatch: Any,
) -> None:
    dispatched: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    sentinel_deferred = object()

    def _fake_defer_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        dispatched.append((fn, args, kwargs))
        return sentinel_deferred

    monkeypatch.setattr(db_mod, "deferToThread", _fake_defer_to_thread)

    invoked: list[Any] = []

    def _work(a: int, b: int, *, kw: str | None = None) -> int:
        invoked.append((a, b, kw))
        return a + b

    result = db_mod.run_in_thread(_work, 1, 2, kw="x")

    assert result is sentinel_deferred
    assert dispatched == [(_work, (1, 2), {"kw": "x"})]
    # `run_in_thread` only ever hands the callable to the (mocked)
    # thread-pool seam -- it never executes it directly on the calling
    # (reactor) thread itself.
    assert invoked == []


def test_run_in_thread_returns_a_real_deferred_via_the_actual_seam() -> None:
    """Sanity check against the real (non-mocked) `deferToThread` -- confirms
    the return type without waiting on/depending on a running reactor loop
    (the offloaded call may or may not have completed by the time this
    assertion runs; only the synchronous return contract is checked)."""
    from twisted.internet.defer import Deferred

    result = db_mod.run_in_thread(lambda: 42)

    assert isinstance(result, Deferred)
