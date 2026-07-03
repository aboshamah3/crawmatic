"""Alerts endpoints (`contracts/api-alerts.md`) — SPEC-09 US2 T025.

`GET /v1/alerts/current` (+ `/{variant_id}`) and `GET /v1/alert-events` —
workspace-scoped, `alerts:read`-gated, cursor-paginated reads over
`variant_alert_states` / `price_alert_events`. On the same
`get_current_principal` auth seam as every other router (RLS already
set on the yielded session); every read goes through
`app_shared.repository.scoped_select` with RLS as the second isolation
layer. Never imports `apps/workers` (Constitution I) — the recompute
task that populates these tables is enqueued by name elsewhere
(`app_shared.messaging`, US3).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.enums import AlertSeverity, AlertType
from app_shared.models.alerts import PriceAlertEvent, VariantAlertState
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_select

from app.deps import Principal, require_scopes
from app.schemas.alerts import (
    AlertEventListResponse,
    AlertEventResponse,
    AlertStateListResponse,
    AlertStateResponse,
)

router = APIRouter(tags=["alerts"])


def _invalid_cursor(exc: InvalidCursor) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
    )


@router.get("/v1/alerts/current", response_model=AlertStateListResponse)
def list_current_alerts(
    limit: int | None = None,
    cursor: str | None = None,
    type: AlertType | None = None,  # noqa: A002 - query param name per contract.
    severity: AlertSeverity | None = None,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> AlertStateListResponse:
    """`GET /v1/alerts/current` — paginated, filterable by `type`/`severity`."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(VariantAlertState, principal.workspace_id)
    if type is not None:
        stmt = stmt.where(VariantAlertState.type == type)
    if severity is not None:
        stmt = stmt.where(VariantAlertState.severity == severity)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise _invalid_cursor(exc) from exc
        stmt = stmt.where(keyset_predicate(VariantAlertState, after))
    stmt = stmt.order_by(VariantAlertState.created_at, VariantAlertState.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [AlertStateResponse.model_validate(row) for row in envelope["items"]]
    return AlertStateListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/v1/alerts/current/{variant_id}", response_model=AlertStateResponse)
def get_current_alert(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> AlertStateResponse:
    """`GET /v1/alerts/current/{variant_id}` — the one current alert row, or 404."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    alert_state = session.execute(
        scoped_select(VariantAlertState, principal.workspace_id).where(
            VariantAlertState.product_variant_id == variant_id
        )
    ).scalar_one_or_none()
    if alert_state is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "No current alert for this variant.",
                }
            },
        )
    return AlertStateResponse.model_validate(alert_state)


@router.get("/v1/alert-events", response_model=AlertEventListResponse)
def list_alert_events(
    limit: int | None = None,
    cursor: str | None = None,
    variant_id: uuid.UUID | None = None,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> AlertEventListResponse:
    """`GET /v1/alert-events` — paginated, filterable by `variant_id`."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(PriceAlertEvent, principal.workspace_id)
    if variant_id is not None:
        stmt = stmt.where(PriceAlertEvent.product_variant_id == variant_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise _invalid_cursor(exc) from exc
        stmt = stmt.where(keyset_predicate(PriceAlertEvent, after))
    stmt = stmt.order_by(PriceAlertEvent.created_at, PriceAlertEvent.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [AlertEventResponse.model_validate(row) for row in envelope["items"]]
    return AlertEventListResponse(items=items, next_cursor=envelope["next_cursor"])
