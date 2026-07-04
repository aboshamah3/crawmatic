"""Non-blocking reactor delay helper (contracts/reactor-seam.md; FR-007, SC-005).

The **only** sanctioned way to wait between requeues in the scrape
path: a ``callLater``-backed ``Deferred`` that fires after the
requested delay while the reactor keeps servicing every other request.
Never ``time.sleep``, never a blocking wait on the reactor thread.
"""

from __future__ import annotations

from twisted.internet import reactor
from twisted.internet.defer import Deferred

__all__ = ["deferred_delay"]


def deferred_delay(seconds: float) -> Deferred:
    """Return a ``Deferred`` that fires after ``seconds`` via ``reactor.callLater``.

    The reactor keeps servicing other requests while this one waits —
    never blocks a thread, never ``time.sleep``. Awaited from the
    spider's ``async def start()``/``errback()`` coroutines
    (``await deferred_delay(...)``); the project runs
    ``AsyncioSelectorReactor``, so awaiting a ``Deferred`` is native
    (SPEC-10 precedent).
    """
    d: Deferred = Deferred()
    reactor.callLater(seconds, d.callback, None)
    return d
