"""Fail-secure, Postgres-authoritative abuse limits for the expensive routes.

EPA W5.5-L1 item 2, engine half.

`app.rate_limit` already exists and is deliberately **fail-open**: it guards
*cost*, per credential, across the whole public API, and turning a Redis blip
into a total outage of a paid API would be worse than a minute of unmetered
reads. That reasoning is correct for what it guards and wrong for what this
module guards.

These are the **abuse-able** surfaces — the ones where a single authenticated
caller can make the platform spend money, spawn work, or export data in bulk:

* the discovery trigger (`POST /v1/strategy/discovery-runs`), which enqueues
  real fetches against somebody else's origin server;
* manual recheck (`POST /v1/jobs/run/...`, `POST /v1/variants/{id}/rescrape`),
  which dispatches scrapes on demand;
* the usage export (`GET /v1/admin/usage`), a bulk read;
* the rest of the SaaS control plane (`/v1/admin/*`), which provisions
  workspaces and mints keys.

For those, a limiter that disappears when its store is unreachable is not a
limiter — an attacker who can cause a store failure gets an unmetered API,
and a system under attack is exactly when store failures happen. So this one
**fails CLOSED**: a store error refuses the request.

STORAGE: Postgres, authoritative, per C3's no-dual-write precedent. Redis at
127.0.0.1:56379 is a cache in this system and a cache never grants — a
counter an attacker can evict is a counter an attacker can reset. One
statement per attempt (`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`),
serialised by the unique index, because read-then-write is a race that two
concurrent callers both win — and a deliberate flood is not where that race
is rare, it is where it is the normal case.

CAPABILITY PROBE: the counter table arrives in a migration this task may not
write (the alembic lane is held). Until it exists the limiter is **INERT**,
not fail-closed — see :func:`_table_available`. Failing closed on a table
that was never created would 503 the entire API the moment this code deploys
ahead of its migration, which is a self-inflicted outage, not a safety
control. Once the table exists, every failure is closed. The probe is
memoised per process and the "missing" answer is logged loudly and once.

IDENTITY: `sha256(credential)`, never the credential — the same rule
`app.rate_limit` states, for the same reason (bucket keys reach logs and
metrics). An unauthenticated request is not counted here; it is about to fail
auth anyway, and spending a database round-trip on it would make
unauthenticated traffic *cheaper* to amplify.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app_shared.database import get_session

from app.rate_limit import rate_limit_identity

logger = logging.getLogger(__name__)

#: The counter table. Created by the migration whose body is staged in
#: `alembic/PENDING_MIGRATION_w55l1.py.txt` (the alembic lane was held by
#: another worker when this landed).
COUNTER_TABLE = "api_abuse_limit_counters"


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


_EXISTS_SQL = text("SELECT to_regclass(:qualified_name) IS NOT NULL").bindparams(
    qualified_name=f"public.{COUNTER_TABLE}"
)

_INCREMENT_SQL = text(
    f"""
    INSERT INTO {COUNTER_TABLE} (bucket_key, window_starts_at, count)
    VALUES (:bucket_key, :window_starts_at, 1)
    ON CONFLICT (bucket_key, window_starts_at)
    DO UPDATE SET count = {COUNTER_TABLE}.count + 1
    RETURNING count
    """
)


class CounterUnavailable(RuntimeError):
    """The counter table exists but could not be written. Refuse the request."""


def _database_is_configured() -> bool:
    """Can this process even construct ``Settings``?

    Not a health check and NOT the fail-closed path. `Settings` failing to
    build means there is no `.env` in this process at all — the shape of a
    unit test that imports the shared `app` object without full config. A
    middleware that turned a missing dev-config file into a 429 on every
    limited route would be a self-inflicted outage of the test suite, and
    `app.rate_limit` already takes exactly this position for exactly this
    reason ("an unconstructable `Settings` disables the limiter rather than
    raising through `dispatch`").

    This is not a hole in production: `app.main` calls
    `assert_production_safe()` at import, so a production API process that
    could not build `Settings` never starts serving at all. Once `Settings`
    exists, every store failure below fails CLOSED.
    """
    try:
        from app_shared.config import get_settings

        return bool(get_settings().DATABASE_URL)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning(
            "abuse limiter disabled: this process cannot construct Settings",
            exc_info=True,
        )
        return False


def _table_available(session) -> bool:
    """Does the counter table exist yet?

    A **capability** answer, not a health answer: ``False`` means "this
    deployment has not run the migration", which makes the limiter inert.
    Any *error* asking the question is treated as ``True`` — the table may
    well be there and we simply could not look, and guessing "absent" would
    silently disable a safety control on a transient blip.
    """
    try:
        return bool(session.execute(_EXISTS_SQL).scalar())
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("abuse-limit capability probe failed; assuming present", exc_info=True)
        return True


def record_attempt(session, *, bucket_key: str, window_starts_at: datetime) -> int | None:
    """Count one attempt and return the resulting count.

    ``None`` means the table does not exist (limiter inert). Raises
    :class:`CounterUnavailable` when it exists and the write failed — the
    caller must refuse.

    The increment happens for EVERY attempt, admitted or refused. If refused
    attempts did not count, a caller sitting at the ceiling would be refused,
    would not increment, and would be admitted on the next request — a limit
    that permanently grants one request per window.
    """
    if not _table_available(session):
        return None
    try:
        count = session.execute(
            _INCREMENT_SQL,
            {"bucket_key": bucket_key, "window_starts_at": window_starts_at},
        ).scalar_one()
        session.commit()
        return int(count)
    except Exception as exc:  # noqa: BLE001 - converted to a refusal by the caller
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - a dead session cannot be rolled back
            pass
        raise CounterUnavailable(str(exc)) from exc


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


class AbuseLimitMiddleware(BaseHTTPMiddleware):
    """Fail-closed, per-credential limits on the abuse-able surfaces."""

    def __init__(
        self,
        app,
        *,
        session_factory: Callable[[], object] | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        # An INJECTED factory is the caller's own store, so the
        # "can this process build Settings?" question does not apply to it —
        # it exists only to decide whether the DEFAULT `get_session` is usable
        # here. Keeping the two apart is what lets the tests exercise the
        # fail-closed branch without a `.env`.
        self._injected_factory = session_factory
        self._session_factory = session_factory or get_session
        self._enabled = enabled

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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._enabled:
            return await call_next(request)
        if self._injected_factory is None and not _database_is_configured():
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
        window_starts_at = window_start_for(now, surface.window_seconds)
        retry_after = surface.window_seconds - (int(now) % surface.window_seconds)
        retry_after = retry_after or surface.window_seconds
        bucket_key = f"abuse:{surface.name}:{identity}"

        try:
            with self._session_factory() as session:
                count = record_attempt(
                    session, bucket_key=bucket_key, window_starts_at=window_starts_at
                )
        except CounterUnavailable as exc:
            logger.error(
                "abuse limiter could not record an attempt for %s; refusing rather "
                "than admitting unlimited traffic. Cause: %s",
                surface.name,
                exc,
            )
            return self._refuse(surface, retry_after, reason="counter-unavailable")
        except Exception:  # noqa: BLE001 - a session we could not even open
            logger.error(
                "abuse limiter could not open a session for %s; refusing.",
                surface.name,
                exc_info=True,
            )
            return self._refuse(surface, retry_after, reason="session-unavailable")

        if count is None:
            # The migration has not run in this deployment. Inert, loudly.
            logger.warning(
                "abuse limiter is INERT: %s does not exist in this database, so "
                "%s is unlimited. Run the pending migration.",
                COUNTER_TABLE,
                surface.name,
            )
            return await call_next(request)

        if count > surface.limit:
            return self._refuse(surface, retry_after, reason="over-limit")

        response = await call_next(request)
        response.headers["X-AbuseLimit-Limit"] = str(surface.limit)
        response.headers["X-AbuseLimit-Remaining"] = str(max(0, surface.limit - count))
        return response
