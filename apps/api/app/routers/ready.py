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

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app_shared import heartbeat as heartbeat_mod
from app_shared import release as release_mod
from app_shared.config import get_settings
from app_shared.costauth.service import DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS
from app_shared.database import get_session
from app_shared.models.proxy_breaker import GLOBAL_BREAKER_SCOPE, ProxyCircuitBreaker
from app_shared.redis_client import get_redis_client

router = APIRouter(tags=["ready"])

#: Per-dependency budget. Generous enough that a healthy PgBouncer/Redis
#: round-trip never trips it, tight enough that a hung dependency does
#: not make an orchestrator's own probe timeout the thing that fires.
_CHECK_TIMEOUT_SECONDS = 2.0


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
    #: Operator-readable summary (A5). Built only from names and counts this
    #: process produced — never from an exception message, a Redis value, or
    #: a connection string. `error` keeps its original discipline: an
    #: exception CLASS NAME and nothing else.
    detail: str | None = None


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, DependencyCheck]


def _run_with_timeout(fn: Callable[[], None], *, timeout: float) -> DependencyCheck:
    """Run `fn` (a no-arg probe) with a hard wall-clock budget.

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
        except Exception as exc:  # noqa: BLE001 - reported as class name only
            return DependencyCheck(ok=False, error=exc.__class__.__name__)
    finally:
        # `wait=False`: on a genuine timeout the submitted call is still
        # running on its worker thread. Waiting here would silently turn
        # the timeout budget back into an unbounded block, defeating the
        # whole point. The worker thread finishes (or stays blocked) on
        # its own; it holds no resource this process doesn't already
        # leak on any other timed-out DB/Redis call.
        pool.shutdown(wait=False)


def _run_check_with_timeout(
    fn: Callable[[], DependencyCheck], *, timeout: float
) -> DependencyCheck:
    """`_run_with_timeout` for a probe that builds its own `DependencyCheck`.

    Same budget, same thread-pool discipline, same "class name only" failure
    reporting; the difference is that these A5 probes have a verdict of their
    own to report (a mismatch is not an exception) rather than signalling
    success by simply not raising.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            return DependencyCheck(
                ok=False, error=f"TimeoutError: exceeded {timeout}s budget"
            )
        except Exception as exc:  # noqa: BLE001 - reported as class name only
            return DependencyCheck(ok=False, error=exc.__class__.__name__)
    finally:
        pool.shutdown(wait=False)


def _check_database(session: Session) -> None:
    """Schema-independent connectivity probe — same query `check_connection()`
    (`app_shared.database`) uses, run inline here so it can share this
    request's already-open session rather than opening a second one."""
    session.execute(text("SELECT 1"))


def _check_redis(client: Any) -> None:
    client.ping()


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


def _breaker_enabled() -> bool:
    """`PROXY_BREAKER_ENABLED`, without requiring a COMPLETE `Settings`.

    Task H2's lesson, applied one module over: a probe that constructs the
    whole settings object inherits every unrelated required field, and a
    process that is missing one then reports a `ValidationError` as a
    failed dependency — a config problem masquerading as an outage. The
    field's own default is `True`, and defaulting to "enabled" is the
    fail-safe side of the question: it makes this check DO the freshness
    read rather than silently declare the deadlock somebody else's
    problem.
    """
    try:
        return bool(get_settings().PROXY_BREAKER_ENABLED)
    except Exception:  # noqa: BLE001 - a probe must not depend on full config
        return True


def _check_breaker_evidence(session: Session) -> DependencyCheck:
    """Is the proxy breaker's evidence fresh enough for the cost gate?

    EPA B1. The cost gate fails CLOSED on breaker evidence older than
    `DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`: a stale
    `proxy_circuit_breakers.evaluated_at` denies every paid scrape in the
    fleet. That is a total functional outage of the product's main job,
    and before this check it was completely invisible from the outside —
    every dependency up, every probe green, and not one paid request able
    to run. An instance in that state is NOT ready, so this check
    participates in the 200/503 verdict like every other one.

    It reads the same row the gate reads, so it cannot disagree with it.
    Nothing request-derived reaches the body: `detail` is built from an
    integer this process computed and the row's own enum value.
    """
    if not _breaker_enabled():
        # No breaker means no gate to deadlock; a disabled subsystem is a
        # labelled absence, not a pass and not a failure (this module's
        # docstring, same treatment as undeclared heartbeat services).
        return DependencyCheck(ok=True, detail="disabled")

    row = session.execute(
        select(ProxyCircuitBreaker.evaluated_at, ProxyCircuitBreaker.state).where(
            ProxyCircuitBreaker.scope_key == GLOBAL_BREAKER_SCOPE
        )
    ).first()
    if row is None:
        return DependencyCheck(
            ok=False,
            error="no-row",
            detail="cost gate denies all paid work",
        )

    evaluated_at, state = row[0], row[1]
    # TIMESTAMPTZ everywhere, so a naive value can only come from outside
    # the ORM; assume UTC rather than crash the probe.
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    age_seconds = int((datetime.now(UTC) - evaluated_at).total_seconds())
    stale = age_seconds > DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS
    return DependencyCheck(
        ok=not stale,
        error="stale" if stale else None,
        detail=f"age_seconds={age_seconds} state={getattr(state, 'value', state)}",
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(
    response: Response,
    session: Session = Depends(_get_db_session),
    redis_client: Any = Depends(_get_redis_dependency),
) -> ReadyResponse:
    # Every check is independently timeboxed by the same budget: the two new
    # A5 checks each perform I/O (one system-table read, one Redis SCAN), so
    # a hung dependency must not be able to delay the verdict through them
    # any more than it can through the connectivity probes above.
    checks = {
        "database": _run_with_timeout(
            lambda: _check_database(session), timeout=_CHECK_TIMEOUT_SECONDS
        ),
        "redis": _run_with_timeout(
            lambda: _check_redis(redis_client), timeout=_CHECK_TIMEOUT_SECONDS
        ),
    }

    # The two A5 checks reuse this request's session/client, and each check
    # runs on its own worker thread — but strictly one at a time, because
    # `_run_with_timeout` blocks on `future.result()` before the next check
    # is submitted. The single exception is a TIMED-OUT check: `wait=False`
    # deliberately leaves that worker running (see `_run_with_timeout`), so
    # its `session` is still in use by another thread. A SQLAlchemy `Session`
    # is not thread-safe, so `migrations` is short-circuited rather than
    # allowed to touch a session a stuck thread still holds. Nothing is lost:
    # a failed `database` check has already decided the verdict, and
    # "database unreachable" is the more accurate thing to report than a
    # derived migration error caused by it.
    if checks["database"].ok:
        checks["migrations"] = _run_check_with_timeout(
            lambda: _check_migrations(session), timeout=_CHECK_TIMEOUT_SECONDS
        )
    else:
        checks["migrations"] = DependencyCheck(
            ok=False,
            error="DatabaseUnavailable",
            detail="not checked — the database check failed first",
        )

    # Same session hazard, same short-circuit, same reason as `migrations`
    # above: a timed-out `database` check leaves its worker thread still
    # holding this session, and a `Session` is not thread-safe.
    if checks["database"].ok:
        checks["breaker_evidence"] = _run_check_with_timeout(
            lambda: _check_breaker_evidence(session), timeout=_CHECK_TIMEOUT_SECONDS
        )
    else:
        checks["breaker_evidence"] = DependencyCheck(
            ok=False,
            error="DatabaseUnavailable",
            detail="not checked — the database check failed first",
        )

    # Redis has no equivalent hazard (`redis.Redis` is thread-safe and pools
    # its own connections), but the same short-circuit is applied for the
    # same readability reason: one root cause, reported once.
    if checks["redis"].ok:
        checks["heartbeats"] = _run_check_with_timeout(
            lambda: _check_heartbeats(redis_client), timeout=_CHECK_TIMEOUT_SECONDS
        )
    else:
        checks["heartbeats"] = DependencyCheck(
            ok=False,
            error="RedisUnavailable",
            detail="not checked — the redis check failed first",
        )
    ready_state = all(check.ok for check in checks.values())
    response.status_code = 200 if ready_state else 503
    return ReadyResponse(ready=ready_state, checks=checks)


__all__ = ["router"]
