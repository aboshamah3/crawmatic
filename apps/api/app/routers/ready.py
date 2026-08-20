"""`GET /ready` — readiness probe: 200 only when the API can actually serve.

SPEC-01 (`specs/001-monorepo-skeleton/contracts/health.md`) drew a hard
line: `/health` MUST NOT touch the database, Redis, or Scrapyd — it
reflects only that the process is up, so it returns 200 even before
Postgres accepts connections. That same contract predicted this
endpoint by name: "A readiness check that touches PgBouncer may be
added in a later spec ... any future readiness variant reuses the
process-wide lazy engine" (FR-020). This is that later spec (audit
2026-08-20, prelaunch hardening).

`/ready` is deliberately the opposite of `/health` in every way that
follows from being a *readiness* rather than *liveness* probe:

* It DOES touch the database and Redis — that is the whole point. A
  platform that only ever checks `/health` cannot tell "the process
  forked but Postgres is unreachable" from "everything is fine", and
  will keep routing traffic into an instance that 500s on every
  request.
* It reuses the process-wide lazy singletons from `app_shared.database`
  (`get_session`, same discipline as `/version`) and
  `app_shared.redis_client` (`get_redis_client`) — never a per-request
  engine/client, same FR-020 constraint `/health`'s contract already
  wrote down.
* It is unauthenticated, like `/health` and `/version`: it is a
  platform probe (Railway or any orchestrator hitting it with no
  credential), not a tenant or operator surface, and it leaks nothing
  sensitive — booleans and an exception class name plus a truncated
  message only, exactly `/version`'s discipline (never a connection
  string, password, or traceback). Unlike `/ops/metrics` and
  `/v1/admin/*` it carries no `require_service_token` guard and no
  `include_in_schema=False` — it is not tagged in
  `app.openapi_public.INTERNAL_TAGS`, so (like `/health` and
  `/version`) it stays in the published spec.
* Unlike `/ops/metrics` (always 200 by design, because that surface is
  a *monitor* whose own availability must not be confused with the
  thing it monitors), `/ready` IS the thing an orchestrator acts on:
  200 when every checked dependency answered, 503 when any did not.

Redis "not-configured" is not a reachable state here
-----------------------------------------------------

The general instruction for a readiness probe is that an *unconfigured*
optional dependency should be reported as a labelled absence
(`"not-configured"`) rather than flipping readiness to false. That
state does not exist for this process: `Settings.REDIS_URL` is a
mandatory, non-`Optional[str]` field (`libs/shared/app_shared/config.py`)
with no default, and `app.main` calls `assert_production_safe()` (which
calls `get_settings()`) at import time, before the app object — let
alone this router — exists. `get_settings()` is `@lru_cache`d, so if
`REDIS_URL` were unset the process would already have failed to boot
with a pydantic `ValidationError`, long before `/ready` could ever be
requested. A running process is therefore proof Redis is configured;
the only two states `/ready` can ever observe for it are "reachable"
and "unreachable", so `checks.redis` never carries a `not-configured`
value. (If a future service made Redis genuinely optional, that branch
would need to be added explicitly — see `checks.database` for the
`DependencyCheck` shape it would reuse.)

Timeboxing
----------

A synchronous `SELECT 1` / `PING` against a dependency that is not
merely down but *hanging* (e.g. a half-open TCP connection to a
firewalled host) can block for the OS-level TCP timeout, which is far
longer than any orchestrator's probe budget. Neither the shared engine
nor the shared Redis client is configured with a statement/socket
timeout (checked: no `statement_timeout` or `socket_timeout` anywhere
in this repo), and changing that would touch every other caller of
those singletons. So each check here runs in its own short-lived
single-worker thread pool and is bounded by `future.result(timeout=...)`
independently — one hung dependency cannot delay or hide the verdict on
the other, and the probe itself always returns within
`_CHECK_TIMEOUT_SECONDS` of either check's completion. The pool is shut
down with `wait=False` on timeout so a genuinely stuck call does not
also block the request thread; it is reported as `TimeoutError`, the
same "exception class name, no raw message" discipline as every other
failure here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.database import get_session
from app_shared.redis_client import get_redis_client

router = APIRouter(tags=["ready"])

#: Per-dependency budget. Generous enough that a healthy PgBouncer/Redis
#: round-trip never trips it, tight enough that a hung dependency does
#: not make an orchestrator's own probe timeout the thing that fires.
_CHECK_TIMEOUT_SECONDS = 2.0

#: Same truncation length `/ops/metrics`'s collector failures use for
#: reported exception messages — long enough to be useful, short enough
#: that a message that happened to embed something sensitive can't
#: round-trip whole.
_MAX_ERROR_LENGTH = 200


def _get_db_session() -> Iterator[Session]:
    """A bare DB session dependency, mirroring `app.routers.version._get_db_session`.

    A named FastAPI dependency (rather than calling `get_session()`
    inline) so tests can override it via `app.dependency_overrides`, the
    same pattern every other router in this app uses for its DB
    dependency.
    """
    with get_session() as session:
        yield session


def _get_redis_dependency() -> Any:
    """The process-wide Redis client, as a FastAPI dependency.

    Deliberately a dependency (not a bare module call inline in the
    route, the way `ops_metrics._get_redis` is) so it can be overridden
    with a fake in tests the exact same way `_get_db_session` is —
    consistent treatment for both dependencies this endpoint checks.
    """
    return get_redis_client()


class DependencyCheck(BaseModel):
    ok: bool
    error: str | None = None


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, DependencyCheck]


def _truncate(message: str) -> str:
    if len(message) <= _MAX_ERROR_LENGTH:
        return message
    return message[:_MAX_ERROR_LENGTH] + "…"


def _run_with_timeout(fn: Callable[[], None], *, timeout: float) -> DependencyCheck:
    """Run `fn` (a no-arg probe) with a hard wall-clock budget.

    Never raises: a timeout, a connection error, or any other exception
    all become a failed `DependencyCheck` carrying the exception class
    name plus a truncated message — never a raw traceback, connection
    string, or credential (same discipline as `/version`'s `db_error`
    and `/ops/metrics`'s collector-failure reporting).
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        try:
            future.result(timeout=timeout)
            return DependencyCheck(ok=True)
        except FutureTimeoutError:
            return DependencyCheck(
                ok=False, error=f"TimeoutError: exceeded {timeout}s budget"
            )
        except Exception as exc:  # noqa: BLE001 - reported as class+truncated message only
            return DependencyCheck(
                ok=False, error=f"{exc.__class__.__name__}: {_truncate(str(exc))}"
            )
    finally:
        # `wait=False`: on a genuine timeout the submitted call is still
        # running on its worker thread. Waiting here would silently turn
        # the timeout budget back into an unbounded block, defeating the
        # whole point. The worker thread finishes (or stays blocked) on
        # its own; it holds no resource this process doesn't already
        # leak on any other timed-out DB/Redis call.
        pool.shutdown(wait=False)


def _check_database(session: Session) -> None:
    """Schema-independent connectivity probe — same query `check_connection()`
    (`app_shared.database`) uses, run inline here so it can share this
    request's already-open session rather than opening a second one."""
    session.execute(text("SELECT 1"))


def _check_redis(client: Any) -> None:
    client.ping()


@router.get("/ready", response_model=ReadyResponse)
def ready(
    response: Response,
    session: Session = Depends(_get_db_session),
    redis_client: Any = Depends(_get_redis_dependency),
) -> ReadyResponse:
    checks = {
        "database": _run_with_timeout(
            lambda: _check_database(session), timeout=_CHECK_TIMEOUT_SECONDS
        ),
        "redis": _run_with_timeout(
            lambda: _check_redis(redis_client), timeout=_CHECK_TIMEOUT_SECONDS
        ),
    }
    ready_state = all(check.ok for check in checks.values())
    response.status_code = 200 if ready_state else 503
    return ReadyResponse(ready=ready_state, checks=checks)


__all__ = ["router"]
