"""`GET /admin/alerts/active` — the poll target for the existing ops cron (F22).

EPA B9 (audit §13 Operations). This is the delivery half of the alert
rules `app_shared.opsmetrics.rules` evaluates: the existing
`crawmatic-ops-alerts` channel is fed by a cron that polls an HTTP
endpoint, not by a new push integration or provider — investigated
already by `app_shared.opsmetrics.rules`'s own module docstring
("Railway... cannot alert on a custom metric... a rule expressed
anywhere except inside this codebase would never fire"). This router is
that HTTP endpoint: the same `collect_snapshot` + `evaluate` pipeline
`GET /ops/metrics` already runs, reduced to just the firing alerts, so
the cron does not have to fetch and discard the full fleet snapshot on
every poll.

Auth posture: cross-workspace fleet data (same reasoning as
`ops_metrics.py`'s module docstring), so this reuses the **same**
`app.service_auth.require_service_token` guard as `/ops/metrics` and
`/v1/admin/*` — never the tenant auth seam in `app.deps`. Excluded from
the public OpenAPI document via the `admin` tag
(`app.openapi_public.INTERNAL_TAGS`), the same mechanism
`apps/api/app/routers/admin.py` relies on.

D5 (a later EPA task) adds `GET /admin/scorecard` to this same router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app_shared.opsmetrics import collect_snapshot, evaluate, worst_severity

from app.routers.ops_metrics import (
    _get_ops_session,
    _get_redis,
    _get_scrapyd_status,
    _get_settings_or_none,
)
from app.service_auth import require_service_token

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_service_token)],
    include_in_schema=False,
)


@router.get("/alerts/active")
def active_alerts(session: Session = Depends(_get_ops_session)) -> JSONResponse:
    """Every currently-firing alert, most severe first — the same
    `collect_snapshot`/`evaluate` pipeline `GET /ops/metrics` runs,
    without the full snapshot body: the cron polling this only needs to
    know what is firing, not the whole fleet metric set."""
    redis_client = _get_redis()
    settings = _get_settings_or_none()
    snapshot = collect_snapshot(
        session,
        redis=redis_client,
        settings=settings,
        scrapyd_status=_get_scrapyd_status(settings, redis_client),
    )
    alerts = evaluate(snapshot)
    worst = worst_severity(alerts)
    return JSONResponse(
        {
            "collected_at": snapshot.collected_at.isoformat(),
            "worst_severity": str(worst) if worst else None,
            "alert_count": len(alerts),
            "alerts": [a.as_dict() for a in alerts],
        }
    )


__all__ = ["router"]
