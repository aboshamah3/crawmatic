"""API request thread pool sizing (H2, production-readiness audit).

`anyio.to_thread` runs every sync call FastAPI dispatches to a worker
thread (SQLAlchemy's `Session.execute`, most of this app's DB access)
through a process-wide `anyio.CapacityLimiter`. Left at anyio's default
(40 tokens), that pool can hold far more in-flight requests than the DB
connection pool has room for (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`,
`app_shared.config.Settings`) — those extra requests don't fail, they
just queue invisibly behind a DB connection while holding a thread-pool
slot, moving the real bottleneck somewhere it can't be seen or tuned.

`configure_thread_pool` sets the limiter's `total_tokens` to
`settings.API_THREAD_POOL_SIZE` so the two pools are sized together on
purpose. It's a plain function (not inlined in a startup callback) so a
unit test can call it directly, synchronously, from inside an anyio event
loop without needing a running FastAPI app.
"""

from __future__ import annotations

import anyio.to_thread

from app_shared.config import Settings


def configure_thread_pool(settings: Settings) -> int:
    """Set the anyio worker-thread limiter to `settings.API_THREAD_POOL_SIZE`.

    Must run inside an async context (anyio/asyncio event loop) —
    `anyio.to_thread.current_default_thread_limiter()` creates the
    limiter for the *current* event loop the first time it's called, so
    this needs to run in the same loop FastAPI serves requests on (i.e.
    from a startup hook, not at import time). Returns the value set, so
    callers/tests can assert on it without re-reading `settings`.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = settings.API_THREAD_POOL_SIZE
    return settings.API_THREAD_POOL_SIZE
