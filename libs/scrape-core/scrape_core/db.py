"""The reactor-safe DB seam (contracts/reactor-safe-db.md, research D1).

**Decided once** for the whole scraping runtime (FR-017, Constitution
Principle V): synchronous SQLAlchemy wrapped in Twisted
``deferToThread``, reusing the SPEC-02/03 session/RLS seam
(``app_shared.database.get_session`` + ``set_workspace_context``)
through PgBouncer with the existing small per-process pool. No async DB
stack, no second seam invented elsewhere in ``scrape_core``.

:func:`run_in_thread` is the **only** sanctioned way a
pipeline/middleware performs a DB (or other blocking) call — never call
a synchronous DB commit directly on the Twisted reactor thread.
:func:`workspace_txn` is meant to run **inside** the thread offloaded
by :func:`run_in_thread`, never on the reactor itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

from twisted.internet.defer import Deferred
from twisted.internet.threads import deferToThread

from app_shared.database import get_session, set_workspace_context
from sqlalchemy.orm import Session

_T = TypeVar("_T")

__all__ = ["run_in_thread", "workspace_txn"]


def run_in_thread(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Deferred:
    """Offload ``fn(*args, **kwargs)`` to a reactor thread-pool thread.

    Thin wrapper over ``twisted.internet.threads.deferToThread`` — the
    single sanctioned seam through which any pipeline/middleware in
    this package performs a DB (or other blocking) call. Returns a
    ``Deferred`` that fires with ``fn``'s return value (or its
    exception, wrapped in a ``Failure``); never blocks the calling
    (reactor) thread itself.
    """
    return deferToThread(fn, *args, **kwargs)


@contextmanager
def workspace_txn(workspace_id: uuid.UUID | str) -> Iterator[Session]:
    """Yield a workspace-scoped :class:`~sqlalchemy.orm.Session` for one transaction.

    Opens :func:`app_shared.database.get_session`, calls
    :func:`app_shared.database.set_workspace_context` (activating RLS
    for the transaction via ``SET LOCAL app.workspace_id = ...``),
    yields the session, commits on clean exit / rolls back on
    exception, and closes the session either way.

    Must run **inside** a thread already offloaded via
    :func:`run_in_thread` — this function performs a blocking DB round
    trip itself and must never be called directly on the reactor
    thread.
    """
    with get_session() as session:
        set_workspace_context(session, workspace_id)
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
        else:
            session.commit()
