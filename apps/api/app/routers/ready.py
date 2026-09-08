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
  sensitive — booleans and an exception CLASS NAME only, never the
  exception's message, exactly `/version`'s `db_error` discipline
  (`db_error = exc.__class__.__name__`). Truncating the message instead
  would not be enough: a DSN is at the FRONT of a driver's error text
  (`postgresql://user:password@host/db: could not connect`), so keeping
  the first N characters keeps precisely the part that must not be
  published on an unauthenticated endpoint. Unlike `/ops/metrics` and
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

Two further checks, added by READY-001 / Task A5
------------------------------------------------

`PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5 requires
that "`/ready` returns 503 on mismatch" between the migration head the
running code expects and the one the live database reports, and that
"`/ready` aggregates per-instance freshness" of worker/scheduler heartbeats.

* **`checks.migrations`** compares `app_shared.release.code_migration_head()`
  (the running image's Alembic script directory) against
  `alembic_version.version_num` read at request time. This is the same
  comparison `/version` *reports*; the difference is that `/ready` *acts* on
  it. An instance running code that expects a schema the database does not
  have is not ready to serve, even though both Postgres and Redis answer
  perfectly — which is exactly the failure `/version` alone could only
  describe after someone thought to look.

  It is **fail-closed on an unresolved head**: if either side cannot be
  determined, the check fails with a distinct error name rather than passing.
  "I cannot verify that my code matches the live schema" is not readiness,
  and the two ways it can happen are both real defects worth surfacing — an
  image built without `alembic/` (a build regression; `.dockerignore`
  deliberately keeps it, and `apps/migrate/Dockerfile` documents needing it
  at runtime), or an `alembic_version` table that the `migrate` job never
  populated (a deploy-order violation — see `docs/DEPLOY-ROLLBACK.md`, where
  "migrate first, always" is the whole first section). Note this cannot
  strand a *healthy* deploy: whenever the database itself is unreachable the
  `database` check has already failed the probe on its own.

* **`checks.heartbeats`** aggregates `heartbeat:{service}:{instance_id}`
  freshness via `app_shared.heartbeat`, per instance rather than as one
  global per-service flag, with fencing so a zombie process cannot keep a
  service looking healthy. See that module's docstring for why the naive
  single-key design reports healthy in three different situations.

  Unlike Redis, "not configured" IS a reachable state here: no service
  publishes heartbeats until Task A5 Step 7's deploy wires
  `READY_REQUIRED_HEARTBEAT_SERVICES`. That state is reported as a labelled
  absence (`ok=True`, `detail="not-configured"`) rather than either a
  failure — which would take every API instance out of rotation for a
  monitoring feature that is not switched on yet — or a silent pass, which
  would hide that the aggregation is doing nothing.

`checks` therefore now carries four entries rather than two. `DependencyCheck`
gains an optional `detail` field for the operator-readable summary the
heartbeat aggregation produces; `error` keeps its exact previous meaning and
discipline (an exception CLASS NAME, never a message).

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
also block the request thread; it is reported as `TimeoutError` plus
the budget it exceeded — a sentence this file writes itself, containing
nothing from the failed call — the same "exception class name, no raw
message" discipline as every other failure here.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared import heartbeat as heartbeat_mod
from app_shared import release as release_mod
from app_shared.database import get_session
from app_shared.redis_client import get_redis_client

router = APIRouter(tags=["ready"])

#: Per-dependency budget. Generous enough that a healthy PgBouncer/Redis
#: round-trip never trips it, tight enough that a hung dependency does
#: not make an orchestrator's own probe timeout the thing that fires.
_CHECK_TIMEOUT_SECONDS = 2.0

#: EPA B8 (F16): ONE bounded, process-wide pool for every probe this router
#: ever runs — never a per-call pool. B7's driver-level timeouts (an
#: earlier wave) make the previous `wait=False`-and-discard pattern
#: unnecessary: a probe that is merely slow finishes and frees its worker;
#: a probe whose underlying driver call is genuinely stuck is bounded by
#: that driver-level timeout, not by this pool. `max_workers=2` is a
#: resource ceiling, not a concurrency target — see `_generation_lock`
#: below for why concurrent `/ready` requests do not each get their own
#: probes.
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=2)

#: Guards a full probe "generation" (one pass over every check). Held for
#: the duration of computing a fresh `ReadyResponse`. A request that finds
#: this already held does NOT wait behind it and does NOT submit its own
#: probes — every dependency probe is I/O, and an orchestrator hammering
#: `/ready` while a dependency is slow must not turn into N times as much
#: load on that dependency. It gets the previous generation's result
#: instead, marked `stale=True` (F16).
_generation_lock = threading.Lock()

#: The last completed generation's result + status code, read by any
#: request that loses the race for `_generation_lock`. `None` until the
#: first generation ever completes.
_cache_lock = threading.Lock()
_last_response: "ReadyResponse | None" = None
_last_status_code: int = 503


class DependencyCheck(BaseModel):
    ok: bool
    error: str | None = None
    #: Operator-readable summary (A5). Built only from names and counts this
    #: process produced — never from an exception message, a Redis value, or
    #: a connection string. `error` keeps its original discipline: an
    #: exception CLASS NAME and nothing else.
    detail: str | None = None


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, DependencyCheck]
    #: EPA B8 (F16): true when this body is the previous generation's
    #: cached result, returned because a generation was already in flight
    #: when this request arrived rather than recomputed on this request's
    #: behalf. A stale body can still be trusted for its own probe ages —
    #: it is simply not THIS request's own round trip.
    stale: bool = False


def _await_probe(future: "Future[DependencyCheck]", *, timeout: float) -> DependencyCheck:
    """Wait for a probe already submitted to `_PROBE_EXECUTOR`, bounded by
    `timeout`.

    Never raises: a timeout, a connection error, or any other exception
    all become a failed `DependencyCheck` carrying the exception CLASS
    NAME and nothing else — never the exception's message, and so never
    a traceback, connection string, or credential. `str(exc)` is not
    ours to publish: a psycopg/SQLAlchemy connection failure puts the
    whole DSN, password included, in the first few characters of its
    message, and this endpoint is unauthenticated. The class name is
    what an orchestrator or on-call reader actually acts on ("is it
    OperationalError or TimeoutError?"); the message belongs in the
    server log, which is already where the traceback goes. Same
    discipline as `/version`'s `db_error`.

    Unlike the pre-B8 per-check pool, a timeout here does NOT shut down or
    discard anything: `_PROBE_EXECUTOR` is shared and long-lived, and B7's
    driver-level statement/socket timeouts (an earlier wave) already bound
    how long the submitted call itself can run — this timeout is a second,
    independent guard on how long THIS request waits for it, not the only
    thing standing between a hung driver call and a wedged worker.
    """
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        return DependencyCheck(ok=False, error=f"TimeoutError: exceeded {timeout}s budget")
    except Exception as exc:  # noqa: BLE001 - reported as class name only
        return DependencyCheck(ok=False, error=exc.__class__.__name__)


def _probe_database() -> DependencyCheck:
    """Schema-independent connectivity probe — same query `check_connection()`
    (`app_shared.database`) uses. EPA B8: opens its OWN session via
    `get_session()` rather than sharing this request's session, so a
    request handler is never the only thing keeping a probe's session
    alive across generations."""
    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
        return DependencyCheck(ok=True)
    except Exception as exc:  # noqa: BLE001 - reported as class name only
        return DependencyCheck(ok=False, error=exc.__class__.__name__)


def _probe_redis() -> DependencyCheck:
    try:
        get_redis_client().ping()
        return DependencyCheck(ok=True)
    except Exception as exc:  # noqa: BLE001 - reported as class name only
        return DependencyCheck(ok=False, error=exc.__class__.__name__)


def _live_migration_head(session: Session) -> str | None:
    """`alembic_version.version_num`, or ``None`` if it can't be read.

    Reuses this request's already-open session (same reasoning as
    `_check_database`), and asks exactly the question
    `apps/api/app/routers/version.py` asks — one system table, never tenant
    data, so no RLS GUC and no auth seam are involved.
    """
    row = session.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def _check_migrations(session: Session) -> DependencyCheck:
    """Fail-closed comparison of code-expected vs live migration head.

    Never raises: any error becomes a failed check carrying the exception
    CLASS NAME only, exactly as `_run_with_timeout` does for the
    connectivity probes.
    """
    try:
        live = _live_migration_head(session)
    except Exception as exc:  # noqa: BLE001 - class name only, never the message
        return DependencyCheck(
            ok=False, error=exc.__class__.__name__, detail="live migration head unreadable"
        )

    identity = release_mod.get_release_identity(live_db_migration=live)
    expected = identity.expected_db_migration

    if expected is None:
        return DependencyCheck(
            ok=False,
            error="ExpectedMigrationUnresolved",
            detail="running code's alembic script directory could not be resolved",
        )
    if live is None:
        return DependencyCheck(
            ok=False,
            error="LiveMigrationUnresolved",
            detail="alembic_version is empty — has the migrate job run?",
        )
    if expected != live:
        # Neither revision id is published here. They are not secrets, and
        # `/version` prints both on purpose — but `/ready` is consumed by an
        # orchestrator that acts on the status code, and keeping this body
        # to fixed strings means no request-time value can ever reach it.
        return DependencyCheck(
            ok=False,
            error="MigrationHeadMismatch",
            detail="code-expected and live migration heads differ — see /version",
        )
    return DependencyCheck(ok=True, detail="code-expected and live migration heads match")


def _check_heartbeats(client: Any) -> DependencyCheck:
    """Per-instance freshness across every declared service.

    "No services declared" is a labelled absence, not a pass and not a
    failure — see this module's docstring.
    """
    services = heartbeat_mod.required_heartbeat_services()
    if not services:
        return DependencyCheck(ok=True, detail="not-configured")

    fleet = heartbeat_mod.aggregate_fleet_freshness(
        client, services, min_instances=heartbeat_mod.required_min_instances()
    )
    failing = [name for name, freshness in fleet.items() if not freshness.ok]
    detail = "; ".join(
        freshness.detail or name for name, freshness in sorted(fleet.items())
    )
    if failing:
        first_error = next(
            (fleet[name].error for name in failing if fleet[name].error), None
        )
        return DependencyCheck(
            ok=False, error=first_error or "StaleHeartbeat", detail=detail
        )
    return DependencyCheck(ok=True, detail=detail)


def _probe_migrations() -> DependencyCheck:
    """`_check_migrations`, opening its OWN session (EPA B8/F16).

    Never shares a session object with `_probe_database` — each probe in
    this router now opens exactly one `get_session()` of its own, so a
    session held by a stuck probe can never be touched by another one
    (the hazard the pre-B8 short-circuiting comments here used to guard
    against by sharing a single request-scoped session instead).
    """
    try:
        with get_session() as session:
            return _check_migrations(session)
    except Exception as exc:  # noqa: BLE001 - class name only, never the message
        return DependencyCheck(
            ok=False, error=exc.__class__.__name__, detail="live migration head unreadable"
        )


def _probe_heartbeats() -> DependencyCheck:
    try:
        return _check_heartbeats(get_redis_client())
    except Exception as exc:  # noqa: BLE001 - reported as class name only
        return DependencyCheck(ok=False, error=exc.__class__.__name__)


def _compute_ready_response() -> tuple[ReadyResponse, int]:
    """Run one full probe generation and return the fresh, non-stale result.

    EPA B8 (F16): breaker evidence and freshness moved OUT of `/ready` and
    into `GET /health/scraping` (`apps.api.app.routers.health`) — a
    scraping-pipeline signal never gates readiness. `/ready` stays
    DB/Redis/migration/heartbeats only; `READY_REQUIRED_HEARTBEAT_SERVICES`
    stays here because a missing scheduler heartbeat is a dependency
    failure, not a scraping-quality signal.

    Every probe opens its OWN `get_session()`/`get_redis_client()` and is
    submitted to the shared, bounded `_PROBE_EXECUTOR` — never more than
    `max_workers=2` probe threads exist for this whole router, no matter
    how many `/ready` requests are in flight (see `_generation_lock`,
    which ensures only one generation ever runs at a time).
    """
    checks: dict[str, DependencyCheck] = {}

    checks["database"] = _await_probe(
        _PROBE_EXECUTOR.submit(_probe_database), timeout=_CHECK_TIMEOUT_SECONDS
    )
    checks["redis"] = _await_probe(
        _PROBE_EXECUTOR.submit(_probe_redis), timeout=_CHECK_TIMEOUT_SECONDS
    )

    if checks["database"].ok:
        checks["migrations"] = _await_probe(
            _PROBE_EXECUTOR.submit(_probe_migrations), timeout=_CHECK_TIMEOUT_SECONDS
        )
    else:
        checks["migrations"] = DependencyCheck(
            ok=False,
            error="DatabaseUnavailable",
            detail="not checked — the database check failed first",
        )

    if checks["redis"].ok:
        checks["heartbeats"] = _await_probe(
            _PROBE_EXECUTOR.submit(_probe_heartbeats), timeout=_CHECK_TIMEOUT_SECONDS
        )
    else:
        checks["heartbeats"] = DependencyCheck(
            ok=False,
            error="RedisUnavailable",
            detail="not checked — the redis check failed first",
        )

    ready_state = all(check.ok for check in checks.values())
    return ReadyResponse(ready=ready_state, checks=checks), (200 if ready_state else 503)


def _get_cached() -> "tuple[ReadyResponse, int] | None":
    with _cache_lock:
        if _last_response is None:
            return None
        return _last_response, _last_status_code


def _set_cached(resp: ReadyResponse, status_code: int) -> None:
    global _last_response, _last_status_code
    with _cache_lock:
        _last_response = resp
        _last_status_code = status_code


@router.get("/ready", response_model=ReadyResponse)
def ready(response: Response) -> ReadyResponse:
    """EPA B8 (F16): at most one probe generation runs at a time.

    A request that acquires `_generation_lock` computes a fresh result and
    caches it. A request that finds the lock already held returns the
    cached result (if any) with `stale=True` instead of piling its own
    probes on top of the in-flight generation — see `_generation_lock`'s
    docstring. The one case with no cached result to fall back on (the
    very first request(s) this process ever serves) blocks for the
    in-flight generation instead of fabricating a verdict.
    """
    acquired = _generation_lock.acquire(blocking=False)
    if not acquired:
        cached = _get_cached()
        if cached is not None:
            cached_resp, cached_status = cached
            response.status_code = cached_status
            return ReadyResponse(
                ready=cached_resp.ready, checks=cached_resp.checks, stale=True
            )
        # No prior generation exists yet — wait for the in-flight one
        # rather than guess. This only happens for the first request(s) a
        # cold process serves before any generation has ever completed.
        _generation_lock.acquire(blocking=True)
        acquired = True

    try:
        resp, status_code = _compute_ready_response()
        _set_cached(resp, status_code)
    finally:
        _generation_lock.release()

    response.status_code = status_code
    return resp


__all__ = ["router"]
