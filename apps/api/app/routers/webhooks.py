"""Webhook events poll API + endpoint CRUD (`contracts/rest-api.md`) —
SPEC-16 US1 T015/T016, US2 T022-T025.

`GET /v1/webhook-events` (+ `/{id}`) — workspace-scoped,
`webhooks:read`-gated, cursor-paginated reads over `webhook_events`. On
the same `get_current_principal` auth seam as every other router (RLS
already set on the yielded session); every read goes through
`app_shared.repository.scoped_select`/`scoped_get` with RLS as the
second isolation layer. Never imports `apps/workers` (Constitution I)
— the `create_webhook_event` task that populates this table is
enqueued by name elsewhere (`app_shared.messaging`, US3).

`/v1/webhook-endpoints*` (US2) is full tenant CRUD over
`webhook_endpoints` on the same auth seam: `webhooks:write` for
create/update/delete, `webhooks:read` for reads. `url` is SSRF-validated
at save time (create + update) via the existing
`app_shared.url_safety.validate_competitor_url` (string/literal check,
no DNS resolution by design — DNS re-resolution is deferred to
delivery-time, out of v1 scope); `UnsafeUrlError` -> `422 UNSAFE_URL`,
nothing persisted (mirrors `apps/api/app/routers/matches.py`'s
`_unsafe_url`). An optional plaintext `secret` is encrypted via
`app_shared.security.encryption.encrypt_secret` before storage and
never returned — responses are always built via
`app.schemas.webhooks._to_response` (explicit field mapping), never
`model_validate(orm_obj)`, mirroring the SPEC-10 `ProxyProvider`
convention.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.models.webhooks import WebhookEndpoint, WebhookEvent
from app_shared.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    keyset_predicate,
    paginate,
)
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.encryption import encrypt_secret
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from app.deps import Principal, require_scopes
from app.schemas.webhooks import (
    WebhookEndpointCreate,
    WebhookEndpointListResponse,
    WebhookEndpointResponse,
    WebhookEndpointUpdate,
    WebhookEventListResponse,
    WebhookEventResponse,
    _to_response,
)

router = APIRouter(tags=["webhooks"])


def _invalid_cursor(exc: InvalidCursor) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
    )


def _not_found(message: str = "Webhook event not found.") -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "NOT_FOUND", "message": message}},
    )


def _unsafe_url(exc: UnsafeUrlError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "UNSAFE_URL",
                "message": str(exc),
                "reason": exc.reason.value,
            }
        },
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


# --- webhook-endpoints CRUD (US2, T022-T025) --------------------------------


@router.post(
    "/v1/webhook-endpoints", response_model=WebhookEndpointResponse, status_code=201
)
def create_webhook_endpoint(
    payload: WebhookEndpointCreate,
    principal_ctx: tuple = Depends(require_scopes("webhooks:write")),
) -> WebhookEndpointResponse:
    """`POST /v1/webhook-endpoints` — SSRF-validate `url`, encrypt `secret`
    if present, persist scoped to the caller's workspace (FR-002/003/005)."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    try:
        validate_competitor_url(payload.url)
    except UnsafeUrlError as exc:
        raise _unsafe_url(exc) from exc

    secret_encrypted: str | None = None
    secret_key_version: int | None = None
    if payload.secret is not None:
        secret = encrypt_secret(payload.secret)
        secret_encrypted = secret.ciphertext
        secret_key_version = secret.key_version

    endpoint = WebhookEndpoint(
        workspace_id=principal.workspace_id,
        name=payload.name,
        url=payload.url,
        enabled=payload.enabled,
        event_types=list(payload.event_types),
        secret_encrypted=secret_encrypted,
        secret_key_version=secret_key_version,
    )
    session.add(endpoint)
    session.flush()

    return _to_response(endpoint)


@router.get("/v1/webhook-endpoints", response_model=WebhookEndpointListResponse)
def list_webhook_endpoints(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("webhooks:read")),
) -> WebhookEndpointListResponse:
    """`GET /v1/webhook-endpoints` — keyset-paginated over `(created_at, id)`."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(WebhookEndpoint, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise _invalid_cursor(exc) from exc
        stmt = stmt.where(keyset_predicate(WebhookEndpoint, after))
    stmt = stmt.order_by(WebhookEndpoint.created_at, WebhookEndpoint.id).limit(
        page_limit + 1
    )

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [_to_response(row) for row in envelope["items"]]
    return WebhookEndpointListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/v1/webhook-endpoints/{id}", response_model=WebhookEndpointResponse)
def get_webhook_endpoint(
    id: uuid.UUID,  # noqa: A002 - path param name per contract.
    principal_ctx: tuple = Depends(require_scopes("webhooks:read")),
) -> WebhookEndpointResponse:
    """`GET /v1/webhook-endpoints/{id}` — not in workspace -> 404."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    endpoint = scoped_get(session, WebhookEndpoint, id, principal.workspace_id)
    if endpoint is None:
        raise _not_found("Webhook endpoint not found.")
    return _to_response(endpoint)


@router.patch("/v1/webhook-endpoints/{id}", response_model=WebhookEndpointResponse)
def update_webhook_endpoint(
    id: uuid.UUID,  # noqa: A002 - path param name per contract.
    payload: WebhookEndpointUpdate,
    principal_ctx: tuple = Depends(require_scopes("webhooks:write")),
) -> WebhookEndpointResponse:
    """`PATCH /v1/webhook-endpoints/{id}` — partial update; `url` re-validated
    when present; `secret` is tri-state (omitted=unchanged, null=clear,
    value=re-encrypt); `updated_at` advances via `TimestampMixin.onupdate`
    (FR-004). Not in workspace -> 404."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    endpoint = scoped_get(session, WebhookEndpoint, id, principal.workspace_id)
    if endpoint is None:
        raise _not_found("Webhook endpoint not found.")

    updates = payload.model_dump(exclude_unset=True)

    if "url" in updates:
        try:
            validate_competitor_url(updates["url"])
        except UnsafeUrlError as exc:
            raise _unsafe_url(exc) from exc

    # `secret` needs bespoke tri-state handling: omitted -> unchanged (not
    # in `updates`); `None` -> clear both ciphertext columns; a non-null
    # string -> re-encrypt under the primary keyring version.
    if "secret" in updates:
        plaintext = updates.pop("secret")
        if plaintext is None:
            endpoint.secret_encrypted = None
            endpoint.secret_key_version = None
        else:
            secret = encrypt_secret(plaintext)
            endpoint.secret_encrypted = secret.ciphertext
            endpoint.secret_key_version = secret.key_version

    for field, value in updates.items():
        setattr(endpoint, field, value)

    session.flush()

    return _to_response(endpoint)


@router.delete("/v1/webhook-endpoints/{id}", status_code=204)
def delete_webhook_endpoint(
    id: uuid.UUID,  # noqa: A002 - path param name per contract.
    principal_ctx: tuple = Depends(require_scopes("webhooks:write")),
) -> None:
    """`DELETE /v1/webhook-endpoints/{id}` — hard delete; 204 on success; not
    in workspace -> 404 (subsequent get/list absent)."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    endpoint = scoped_get(session, WebhookEndpoint, id, principal.workspace_id)
    if endpoint is None:
        raise _not_found("Webhook endpoint not found.")

    session.delete(endpoint)
    session.flush()
    return None
