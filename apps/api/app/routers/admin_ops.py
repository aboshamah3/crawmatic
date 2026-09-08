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

EPA D5 (deep dive §12 item 9) adds two more routes to this same router,
same guard, same exclusion from the public OpenAPI document:

* `GET /admin/scorecard?days=30` — the last `days` already-written
  `fleet_daily_scorecard` rows (newest first). Read-only; the row for a
  given day either exists (written by `MAINTENANCE_DAILY_SCORECARD`) or
  is simply absent from the response — never fabricated here.
* `POST /admin/ops/backup-report` — the receiver C10's
  `scripts/dr/dr_lib.sh:dr_post_backup_report` posts to, fail-soft on
  its end (a non-2xx here just means "kept locally, logged a WARN", per
  that function's own docstring). Folds an accepted report into its
  day's Redis aggregate (`app_shared.maintenance.scorecard.
  record_backup_report`) so `fleet_daily_scorecard.backup_egress_gb`
  has an input to read on the next scorecard run. Schema-versioned
  (`crawmatic.backup-report.v1`) so a shape this receiver predates is
  rejected rather than silently mis-recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app_shared.maintenance.scorecard import (
    BACKUP_REPORT_SCHEMA,
    read_scorecard_range,
    record_backup_report,
)
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


@router.get("/scorecard")
def scorecard(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(_get_ops_session),
) -> JSONResponse:
    """The last `days` already-written daily scorecard rows, newest
    first (EPA D5, deep dive §12 item 9).

    `until` is TODAY UTC, not yesterday: today's row simply is not
    written yet (`MAINTENANCE_DAILY_SCORECARD` writes yesterday's), so
    it is silently absent from `rows` rather than special-cased here —
    the same "an absent row says nothing was measured yet" discipline
    the scorecard table itself follows for a NULL column.
    """
    until = datetime.now(timezone.utc).date()
    since = until - timedelta(days=days - 1)
    rows = read_scorecard_range(session, since, until)
    return JSONResponse(
        {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days_requested": days,
            "days_available": len(rows),
            "rows": [row.as_dict() for row in rows],
        }
    )


@router.post("/ops/backup-report", status_code=202)
def backup_report(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Receive one C10 measured-backup-report POST (EPA D5, deep dive
    §12 item 9, closing the BLOCKERS.md 2026-09-08 C10 follow-up).

    `scripts/dr/dr_lib.sh:dr_post_backup_report` is fail-soft on its own
    end (a non-2xx here is a logged WARN there, never a failed backup),
    so this handler mirrors that discipline in the other direction: a
    schema it does not recognise is rejected with 422 (so a future
    payload shape cannot be silently mis-recorded), but a recognised
    payload this process could not fold into Redis (Redis down) still
    returns 202 -- the POST body itself is evidence enough to log even
    when the durable side-channel used for `backup_egress_gb` did not
    take it, and re-raising here would just make the sender retry a
    write that already logged what it needed to.
    """
    if not isinstance(payload, dict) or payload.get("schema") != BACKUP_REPORT_SCHEMA:
        raise HTTPException(
            status_code=422,
            detail=(
                "unrecognized or missing 'schema' "
                f"(expected {BACKUP_REPORT_SCHEMA!r})"
            ),
        )
    recorded = record_backup_report(_get_redis(), payload)
    return JSONResponse(
        {
            "schema": BACKUP_REPORT_SCHEMA,
            "backup_set": payload.get("backup_set"),
            "recorded": recorded,
        },
        status_code=202,
    )


__all__ = ["router"]
