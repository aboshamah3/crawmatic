"""`GET /ops/metrics` — the operational metrics + alert surface (audit §H5).

Audit ref: `CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §H5
("Production observability and automatic stop-loss controls are
insufficient") and §12 ("Dashboards/alerts show queue age, stale jobs,
success by domain, requests/link, proxy spend, Redis memory/evictions,
and browser capacity").

Auth posture, and why it differs from `/health` and `/version`
--------------------------------------------------------------

`/health` and `/version` are unauthenticated because what they reveal is
either nothing (liveness) or already-public build provenance, and an
incident responder must be able to curl them with no credential.

This endpoint is different in kind. It reports **fleet-wide aggregates
across every workspace**: per-domain spend, month-to-date proxy cost,
competitor hostnames, catalog-scale request volumes, queue depth. None
of it is per-tenant row data, but all of it is commercially sensitive
and some of it (spend velocity, breaker state) tells an attacker exactly
how much paid work they can induce before a stop-loss fires.

It is therefore guarded by `app.service_auth.require_service_token` —
the **same** static bearer that guards `/v1/admin/*`, for the same
reason: this is a cross-workspace operator surface, not a tenant one.
That dependency is fail-closed (`SAAS_SERVICE_TOKEN` unset => every
request 401s) and compares with `hmac.compare_digest`, and it returns
the uniform `AUTH_FAILED` envelope that never distinguishes "not
configured" from "wrong token".

Consequences that are deliberate:

* It runs on `get_auth_session()` (BYPASSRLS) because a workspace-scoped
  session would show one tenant's slice of a fleet metric, which is
  worse than useless — it would under-report spend. Every statement is
  unscoped by design (`# noqa: workspace-scope`), exactly the precedent
  `app/routers/admin.py` sets.
* It is excluded from the public OpenAPI document (`include_in_schema=False`),
  like the rest of the internal surface.
* **It emits no secrets.** The snapshot contains counts, rates, hostnames
  and enum names only: no connection strings, no API keys, no proxy
  credentials, no URLs, no workspace ids, no product/competitor row
  data. `DatabaseRoleHealth` reports the connected role *name* and its
  superuser/BYPASSRLS booleans — which is the point of audit C3 — and no
  password. Collector failures are reported as an exception class name
  plus its message, truncated, never a traceback.

Response shapes
---------------

* default / `?format=json` — `{"collected_at", "worst_severity",
  "alerts": [...], "snapshot": {...}}`.
* `?format=prometheus` — Prometheus text exposition (`text/plain`).
  Nothing scrapes it today (Railway has no Prometheus scraper — see
  `docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md`); it exists so adopting a
  scraper later is a configuration change rather than a re-derivation.
* `?emit=true` — additionally writes the snapshot and every firing alert
  to the structured log stream (`ops.snapshot` / `ops.alert`). This is
  what a periodic poller uses so the alerting record lands in Railway's
  log view. It is opt-in so a human refreshing the endpoint does not
  spam the log.

Status code
-----------

Always **200** when the endpoint itself worked, even when CRITICAL
alerts are firing. A non-200 would make the surface unusable behind any
platform health check and would conflate "the monitor is broken" with
"the system is unhealthy" — the exact confusion H5 is about. Severity
lives in the body (`worst_severity`) where a poller can act on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app_shared.config import get_settings
from app_shared.database import get_auth_session
from app_shared.opsmetrics import (
    collect_snapshot,
    emit_snapshot,
    evaluate,
    render_prometheus,
    worst_severity,
)

from app.service_auth import require_service_token

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    dependencies=[Depends(require_service_token)],
    include_in_schema=False,
)


def _get_ops_session() -> Iterator[Session]:
    """A BYPASSRLS, no-workspace-context session.

    Fleet aggregates only (see the module docstring). Mirrors
    `app/routers/admin.py`'s use of the same seam, and is a named
    dependency so tests can override it with `app.dependency_overrides`.
    """
    with get_auth_session() as session:  # noqa: workspace-scope - fleet aggregates
        yield session


def _get_redis() -> Any | None:
    """The process Redis client, or ``None`` if unavailable.

    Best-effort: Redis memory/eviction counters are a nice-to-have and
    must never turn the ops endpoint into a second failure. When this
    returns ``None`` the snapshot still reports the cached
    `redis_policy.last_report()` posture, which is the signal that
    actually matters (audit H2).
    """
    try:
        from app_shared.redis_client import get_redis_client

        return get_redis_client()
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _get_settings_or_none() -> Any | None:
    """``Settings``, or ``None`` when it cannot be constructed.

    `get_settings()` raises a pydantic `ValidationError` on a
    misconfigured environment. That is exactly the deployment state this
    endpoint exists to report on, so letting it propagate would 500 the
    monitor precisely when the thing being monitored is broken —
    the failure mode H5 is about. Losing `settings` only costs the
    breaker's configured ceiling; every other metric is DB-derived.
    """
    try:
        return get_settings()
    except Exception:  # noqa: BLE001 - see docstring
        return None


@router.get("/metrics")
def ops_metrics(
    session: Session = Depends(_get_ops_session),
    format: Literal["json", "prometheus"] = Query(  # noqa: A002 - query param name
        default="json", description="Response encoding."
    ),
    emit: bool = Query(
        default=False,
        description="Also write the snapshot and firing alerts to the structured log.",
    ),
) -> Response:
    snapshot = collect_snapshot(
        session, redis=_get_redis(), settings=_get_settings_or_none()
    )
    alerts = evaluate(snapshot)

    if emit:
        emit_snapshot(snapshot, alerts)

    if format == "prometheus":
        return Response(
            content=render_prometheus(snapshot, alerts),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    worst = worst_severity(alerts)
    return JSONResponse(
        {
            "collected_at": snapshot.collected_at.isoformat(),
            "worst_severity": str(worst) if worst else None,
            "alert_count": len(alerts),
            "alerts": [a.as_dict() for a in alerts],
            "snapshot": snapshot.as_dict(),
        }
    )


__all__ = ["router"]
