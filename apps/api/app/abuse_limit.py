"""Abuse limits for the expensive routes, counted on the async Redis client.

EPA W5.5-L1 item 2, engine half; store moved to Redis by the 2026-10-07 full
risk review (B3, folds in E8).

`app.rate_limit` already exists and is deliberately **fail-open**: it guards
*cost*, per credential, across the whole public API, and turning a Redis blip
into a total outage of a paid API would be worse than a minute of unmetered
reads.

These are the **abuse-able** surfaces — the ones where a single authenticated
caller can make the platform spend money, spawn work, or export data in bulk:

* the discovery trigger (`POST /v1/strategy/discovery-runs`), which enqueues
  real fetches against somebody else's origin server;
* manual recheck (`POST /v1/jobs/run/...`, `POST /v1/variants/{id}/rescrape`),
  which dispatches scrapes on demand;
* the usage export (`GET /v1/admin/usage`), a bulk read;
* the rest of the SaaS control plane (`/v1/admin/*`), which provisions
  workspaces and mints keys.

FAILURE POSTURE (B3 / decision D4):

* **Writes fail CLOSED.** A store error on a POST/PUT/PATCH/DELETE refuses the
  request with 429. An attacker who can cause a store failure must not get an
  unmetered write surface, and a system under attack is exactly when store
  failures happen. This is the Hard-Stop rule of the 2026-10-07 run: do not
  relax it without an owner decision.
* **Reads fail OPEN** (GET/HEAD/OPTIONS), with a warning log. A read on these
  surfaces spawns no work and mints nothing; refusing the SaaS's own admin
  reads because Redis blinked turned a cache outage into a control-plane
  outage.

STORAGE: Redis, through `app.rate_limit.get_async_admission_redis_client()`
(`redis.asyncio`, 0.25 s connect/socket timeouts, one 0.3 s deadline per
admission check) and the same atomic INCR + EXPIRE-on-first-hit Lua script.
The previous store was a Postgres upsert + commit run with a *synchronous*
SQLAlchemy session inside `async def dispatch`: two statements per
`/v1/admin/**` request on the handlers' own connection pool, blocking the
event loop for up to the pool timeout, with every SaaS admin call
serialised on one hot row that was never pruned (E8). The
`api_abuse_limit_counters` table and its migration are left in place, unused
(no destructive migration in this change); nothing writes it any more.

BUCKETS: `abuse:{surface}:{sha256(credential)}[:ws:{workspace_id}]:{window}`.
Requests under `/v1/admin/workspaces/{id}/...` are counted per workspace:
all SaaS→engine admin traffic arrives on ONE service credential, so a
credential-only bucket made every tenant share one 600/min budget and let a
single busy workspace starve the rest. Anything else keeps the
credential-only bucket.

IDENTITY: `sha256(credential)`, never the credential — the same rule
`app.rate_limit` states, for the same reason (bucket keys reach logs and
metrics). An unauthenticated request is not counted here; it is about to fail
auth anyway, and spending a round-trip on it would make unauthenticated
traffic *cheaper* to amplify.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.rate_limit import (
    _REDIS_ADMISSION_DEADLINE_SECONDS,
    _counts_from_script_result,
    _get_incr_and_expire_script,
    get_async_admission_redis_client,
    rate_limit_identity,
)

logger = logging.getLogger(__name__)

#: The former Postgres counter table. Kept (with its migration and ORM model)
#: so no destructive migration is needed; the limiter no longer reads or
#: writes it. Dropping it is a separate, owner-gated change.
COUNTER_TABLE = "api_abuse_limit_counters"

#: Methods that fail OPEN on a store error. Everything else fails closed.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: `/v1/admin/workspaces/{id}` and anything below it. The id must look like
#: an identifier (UUIDs and slugs do); anything else falls back to the
#: credential-only bucket instead of minting a key from arbitrary path text.
_WORKSPACE_PATH = re.compile(r"^/v1/admin/workspaces/([A-Za-z0-9_-]{1,64})(?:/|$)")


@dataclass(frozen=True)
class Surface:
    """One limited surface: what to call it, and how much it may be used.

    PENDING OWNER REVIEW — every number below. They are chosen to be far
    above any believable legitimate use and far below "free", so that
    switching this on cannot refuse an honest caller. Raise or lower them in
    one place; nothing else reads them.
    """

    name: str
    #: Matched against `f"{METHOD} {path}"`, prefix-wise.
    method: str
    path_prefix: str
    limit: int
    window_seconds: int


#: Order matters: the FIRST match wins, so the specific export rule must
#: precede the catch-all `/v1/admin` rule.
SURFACES: tuple[Surface, ...] = (
    # Discovery enqueues real fetches against a third party's origin.
    Surface("discovery", "POST", "/v1/strategy/discovery-runs", 20, 3600),
    # Manual recheck dispatches scrapes on demand.
    Surface("recheck", "POST", "/v1/jobs/run/", 60, 60),
    Surface("recheck", "POST", "/v1/variants/", 60, 60),
    # Bulk read. Generous: this is the SaaS's own metering feed, and
    # throttling it is a silent revenue-loss path (see `app.rate_limit`'s
    # docstring on why the cost limiter exempts /v1/admin entirely).
    Surface("export", "GET", "/v1/admin/usage", 600, 3600),
    # The rest of the control plane: provisioning, key minting, archive.
    Surface("admin", "", "/v1/admin", 600, 60),
)


def surface_for(method: str, path: str) -> Surface | None:
    """The first surface matching this request, or ``None`` if unlimited here."""
    for surface in SURFACES:
        if surface.method and surface.method != method.upper():
            continue
        if path.startswith(surface.path_prefix):
            return surface
    return None


def window_start_for(now: float, window_seconds: int) -> datetime:
    """Truncate ``now`` (epoch seconds) to the start of its fixed window.

    A fixed window admits up to 2x the limit across a boundary. That is a
    known, bounded property, stated here rather than discovered later; the
    sliding alternative needs per-request history these surfaces do not
    justify.
    """
    return datetime.fromtimestamp(
        (int(now) // window_seconds) * window_seconds, tz=timezone.utc
    )


def workspace_for(path: str) -> str | None:
    """The workspace id of a `/v1/admin/workspaces/{id}/...` path, else ``None``."""
    match = _WORKSPACE_PATH.match(path)
    return match.group(1) if match else None


def bucket_key_for(surface: Surface, identity: str, path: str, window_index: int) -> str:
    """The Redis key one attempt is counted against."""
    workspace_id = workspace_for(path)
    scope = f"{identity}:ws:{workspace_id}" if workspace_id else identity
    return f"abuse:{surface.name}:{scope}:{window_index}"


def fails_open(method: str) -> bool:
    """Does a store error ADMIT this request? Reads only (see module docstring)."""
    return method.upper() in _READ_METHODS


def _store_is_configured() -> bool:
    """Can this process even construct ``Settings`` (and so a Redis client)?

    Not a health check and NOT the fail-closed path. `Settings` failing to
    build means there is no `.env` in this process at all — the shape of a
    unit test that imports the shared `app` object without full config. A
    middleware that turned a missing dev-config file into a 429 on every
    limited write would be a self-inflicted outage of the test suite, and
    `app.rate_limit` takes exactly this position for exactly this reason.

    Not a hole in production: `app.main` calls `assert_production_safe()` at
    import, so a production API process that could not build `Settings`
    never starts serving. The middleware asks this ONCE per instance and
    caches the answer — it is never a per-request probe.
    """
    try:
        from app_shared.config import get_settings

        return bool(get_settings().REDIS_URL)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning(
            "abuse limiter disabled: this process cannot construct Settings",
            exc_info=True,
        )
        return False


#: Backward-compatible name (tests outside this module monkeypatch it).
_database_is_configured = _store_is_configured


class AbuseLimitMiddleware(BaseHTTPMiddleware):
    """Per-credential (per-workspace on admin workspace routes) abuse limits.

    Non-blocking: one `redis.asyncio` script call per limited request, under
    a hard deadline. Writes fail closed on a store error; reads fail open.
    """

    def __init__(
        self,
        app,
        *,
        redis_factory: Callable[[], object] | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        # An INJECTED factory is the caller's own store, so the
        # "can this process build Settings?" question does not apply to it.
        self._injected_factory = redis_factory
        self._redis_factory = redis_factory or get_async_admission_redis_client
        self._enabled = enabled
        #: Cached answer of `_store_is_configured` (``None`` = not asked yet).
        self._store_configured: bool | None = None

    def _is_active(self) -> bool:
        if not self._enabled:
            return False
        if self._injected_factory is not None:
            return True
        if self._store_configured is None:
            self._store_configured = _store_is_configured()
        return self._store_configured

    def _refuse(self, surface: Surface, retry_after: int, *, reason: str) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": (
                        f"Rate limit exceeded for {surface.name}: "
                        f"{surface.limit} requests per {surface.window_seconds} seconds."
                    ),
                },
                "detail": {
                    "error": {"code": "RATE_LIMITED", "message": "Too many requests."}
                },
            },
        )

    async def _record_attempt(self, key: str, surface: Surface) -> int:
        """Count one attempt atomically and return the resulting count.

        The increment happens for EVERY attempt, admitted or refused: the
        script INCRs before it compares. If refused attempts did not count, a
        caller sitting at the ceiling would be admitted again next request.
        Raises on any store error; the caller picks the posture.
        """
        redis_client = self._redis_factory()
        script = _get_incr_and_expire_script(redis_client)
        coro = script(keys=[key], args=[surface.window_seconds * 2, surface.limit])
        result = await asyncio.wait_for(coro, timeout=_REDIS_ADMISSION_DEADLINE_SECONDS)
        return _counts_from_script_result(result, 1)[0]

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._is_active():
            return await call_next(request)

        surface = surface_for(request.method, request.url.path)
        if surface is None:
            return await call_next(request)

        identity = rate_limit_identity(request)
        if identity is None:
            # No credential: about to fail auth anyway, and counting it would
            # make unauthenticated traffic cheaper to amplify, not harder.
            return await call_next(request)

        now = time.time()
        window_index = int(now) // surface.window_seconds
        retry_after = surface.window_seconds - (int(now) % surface.window_seconds)
        retry_after = retry_after or surface.window_seconds
        key = bucket_key_for(surface, identity, request.url.path, window_index)

        try:
            count = await self._record_attempt(key, surface)
        except Exception:  # noqa: BLE001 - posture decided by method below
            if fails_open(request.method):
                logger.warning(
                    "abuse limiter store unavailable for %s; admitting READ "
                    "(fail-open for reads only)",
                    surface.name,
                    exc_info=True,
                )
                return await call_next(request)
            logger.error(
                "abuse limiter store unavailable for %s; refusing WRITE rather "
                "than admitting unlimited traffic",
                surface.name,
                exc_info=True,
            )
            return self._refuse(surface, retry_after, reason="counter-unavailable")

        if count > surface.limit:
            return self._refuse(surface, retry_after, reason="over-limit")

        response = await call_next(request)
        response.headers["X-AbuseLimit-Limit"] = str(surface.limit)
        response.headers["X-AbuseLimit-Remaining"] = str(max(0, surface.limit - count))
        return response
