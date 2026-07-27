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

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

from twisted.internet.defer import Deferred
from twisted.internet.threads import deferToThread

from app_shared.database import get_session, set_workspace_context
from sqlalchemy.orm import Session

_T = TypeVar("_T")

__all__ = ["as_awaitable", "await_in_thread", "run_in_thread", "workspace_txn"]


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


def as_awaitable(d: Deferred) -> Any:
    """Make a ``Deferred`` awaitable under whichever machinery drives the caller.

    The HTTP node (``EPollReactor``) drives spider coroutines through
    Twisted's own ``ensureDeferred`` path, where awaiting a raw
    ``Deferred`` is native — returned as-is. The browser node
    (``AsyncioSelectorReactor`` + Scrapy's ``AsyncCrawlerProcess``)
    drives them as **asyncio tasks**, where a raw ``Deferred`` yield
    crashes with ``Task got bad yield`` — there, the running loop is
    detectable and the Deferred is bridged via ``asFuture``.
    """
    if not isinstance(d, Deferred):
        # Already awaitable some other way (a coroutine/Future from a
        # monkeypatched helper in unit tests) -- nothing to bridge.
        return d
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return d
    return d.asFuture(loop)


async def await_in_thread(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """``await``-safe :func:`run_in_thread` — same thread-pool offload,
    usable from coroutines on BOTH scraper nodes (see :func:`as_awaitable`).
    Callers that need the ``Deferred`` API (``addCallback``/``addBoth``,
    e.g. the persistence pipeline) keep using :func:`run_in_thread`."""
    return await as_awaitable(deferToThread(fn, *args, **kwargs))


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
