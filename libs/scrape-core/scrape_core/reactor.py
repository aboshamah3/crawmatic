"""Non-blocking reactor delay helper (contracts/reactor-seam.md; FR-007, SC-005).

The **only** sanctioned way to wait between requeues in the scrape
path: a ``callLater``-backed ``Deferred`` that fires after the
requested delay while the reactor keeps servicing every other request.
Never ``time.sleep``, never a blocking wait on the reactor thread.
"""

from __future__ import annotations

import asyncio

from twisted.internet.defer import Deferred

__all__ = ["AsyncioSafeDeferred", "deferred_delay"]


class AsyncioSafeDeferred(Deferred):
    """A ``Deferred`` that can be awaited from either coroutine driver.

    Scrapy >= 2.13 drives spider coroutines as asyncio tasks (when the
    ``AsyncioSelectorReactor`` is installed), and an asyncio task
    rejects the plain-``Deferred`` await protocol with ``Task got bad
    yield``. Awaiting this subclass converts to an asyncio ``Future``
    (``Deferred.asFuture``) when a loop is running, and falls back to
    the classic Twisted await protocol otherwise. The ``Deferred``
    callback API is untouched, so synchronous callers (robots
    middleware, the persistence pipeline) keep their contract.
    """

    def __await__(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return super().__await__()
        return self.asFuture(loop).__await__()

    @classmethod
    def from_deferred(cls, d: Deferred) -> AsyncioSafeDeferred:
        out = cls()
        d.chainDeferred(out)
        return out


def deferred_delay(seconds: float) -> Deferred:
    """Return a ``Deferred`` that fires after ``seconds`` via ``reactor.callLater``.

    The reactor keeps servicing other requests while this one waits —
    never blocks a thread, never ``time.sleep``. Awaited from the
    spider's ``async def start()``/``errback()`` coroutines
    (``await deferred_delay(...)``); the project runs
    ``AsyncioSelectorReactor``, so awaiting a ``Deferred`` is native
    (SPEC-10 precedent).

    The reactor import is deliberately deferred to call time: importing
    ``twisted.internet.reactor`` at module import installs the platform
    default reactor, and this module is reachable from spider-module
    imports, which Scrapy's crawler-process ``__init__`` performs
    *before* installing ``AsyncioSelectorReactor`` — an import-time
    install therefore aborts every crawl with a reactor mismatch.
    """
    from twisted.internet import reactor

    d: Deferred = AsyncioSafeDeferred()
    reactor.callLater(seconds, d.callback, None)
    return d
