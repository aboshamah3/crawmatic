"""Webhook events poll API (`contracts/rest-api.md`) — SPEC-16 US1 T015/T016.

`GET /v1/webhook-events` (+ `/{id}`) — workspace-scoped,
`webhooks:read`-gated, cursor-paginated reads over `webhook_events`. On
the same `get_current_principal` auth seam as every other router (RLS
already set on the yielded session); every read goes through
`app_shared.repository.scoped_select`/`scoped_get` with RLS as the
second isolation layer. Never imports `apps/workers` (Constitution I)
— the `create_webhook_event` task that populates this table is
enqueued by name elsewhere (`app_shared.messaging`, US3).

Endpoint CRUD (`/v1/webhook-endpoints*`, US2) lands in this same module
in a later phase — the two stories touch disjoint route handlers.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.models.webhooks import WebhookEvent
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.webhooks import WebhookEventListResponse, WebhookEventResponse

router = APIRouter(tags=["webhooks"])


def _invalid_cursor(exc: InvalidCursor) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "NOT_FOUND", "message": "Webhook event not found."}},
    )


@router.get("/v1/webhook-events", response_model=WebhookEventListResponse)
def list_webhook_events(
    limit: int | None = None,
    cursor: str | None = None,
    event_type: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("webhooks:read")),
) -> WebhookEventListResponse:
    """`GET /v1/webhook-events` — paginated, filterable by `event_type` (FR-014)."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(WebhookEvent, principal.workspace_id)
    if event_type is not None:
        stmt = stmt.where(WebhookEvent.event_type == event_type)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise _invalid_cursor(exc) from exc
        stmt = stmt.where(keyset_predicate(WebhookEvent, after))
    stmt = stmt.order_by(WebhookEvent.created_at, WebhookEvent.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [WebhookEventResponse.model_validate(row) for row in envelope["items"]]
    return WebhookEventListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/v1/webhook-events/{id}", response_model=WebhookEventResponse)
def get_webhook_event(
    id: uuid.UUID,  # noqa: A002 - path param name per contract.
    principal_ctx: tuple = Depends(require_scopes("webhooks:read")),
) -> WebhookEventResponse:
    """`GET /v1/webhook-events/{id}` — single fetch; not in workspace -> 404."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    event = scoped_get(session, WebhookEvent, id, principal.workspace_id)
    if event is None:
        raise _not_found()
    return WebhookEventResponse.model_validate(event)
