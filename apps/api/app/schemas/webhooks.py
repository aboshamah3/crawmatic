"""Webhook API DTOs (`contracts/rest-api.md`) — SPEC-16.

Pydantic v2 request/response models for the `/v1/webhook-events*` (US1,
T014) and `/v1/webhook-endpoints*` (US2, T021) routers
(`apps/api/app/routers/webhooks.py`). Kept in `apps/api` (never
`app_shared`) so the framework-agnostic core never depends on Pydantic
— same discipline as `app.schemas.alerts`/`matches`/`catalog`.

`WebhookEventResponse.delivered_at` is always `null` in v1 (FR-010,
SC-007) — the column exists on the ORM model for the future delivery
feature but no write path in this spec ever sets it.

Endpoint CRUD (US2, FR-002/003/004/005): `WebhookEndpointCreate`/
`Update` accept a plaintext `secret` field that is **never** persisted
as-is — the router encrypts it via
`app_shared.security.encryption.encrypt_secret` into
`secret_encrypted`/`secret_key_version` (mirrors the SPEC-10
`ProxyProvider.password` convention). `WebhookEndpointResponse` never
carries `secret_encrypted`/`secret_key_version` — only a derived
`has_secret` boolean, built by `_to_response` via **explicit field
mapping** (never `model_validate(orm_obj)`), so no response path can
ever leak the ciphertext (a guard test enforces this,
`tests/unit/test_webhook_response_guard.py`). `url` is SSRF-validated
at save time (create + update) by the router via the existing
`app_shared.url_safety.validate_competitor_url` — no second validator.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app_shared.models.webhooks import WebhookEndpoint

# Bounds on `event_types` (data-model.md): forward-compatible free-string
# subscription list, bounded so a client can't push an unbounded JSONB blob.
_MAX_EVENT_TYPES = 64
_MAX_EVENT_TYPE_LENGTH = 200


def _validate_event_types(value: list[str]) -> list[str]:
    if len(value) > _MAX_EVENT_TYPES:
        raise ValueError(f"event_types may contain at most {_MAX_EVENT_TYPES} entries")
    for entry in value:
        if len(entry) > _MAX_EVENT_TYPE_LENGTH:
            raise ValueError(
                f"each event_types entry may be at most {_MAX_EVENT_TYPE_LENGTH} characters"
            )
    return value


class WebhookEventResponse(BaseModel):
    """A `webhook_events` row — one recorded domain-change event."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    delivered_at: datetime | None


class WebhookEventListResponse(BaseModel):
    """`GET /v1/webhook-events` — `{items, next_cursor}` envelope."""

    model_config = ConfigDict(from_attributes=True)

    items: list[WebhookEventResponse]
    next_cursor: str | None


# --- WebhookEndpoint (US2, T021) --------------------------------------------


class WebhookEndpointCreate(BaseModel):
    """`POST /v1/webhook-endpoints` request body.

    `secret`, if supplied, is plaintext on the wire only — the router
    encrypts it before storage and never echoes it back (FR-005).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    enabled: bool = True
    event_types: list[str] = Field(default_factory=list)
    secret: str | None = None

    @field_validator("event_types")
    @classmethod
    def _check_event_types(cls, value: list[str]) -> list[str]:
        return _validate_event_types(value)


class WebhookEndpointUpdate(BaseModel):
    """`PATCH /v1/webhook-endpoints/{id}` — every field optional (partial update).

    Distinguishes "omitted" (unchanged) from "explicitly null" via the
    router's `exclude_unset=True` dump: an omitted `secret` leaves the
    stored ciphertext untouched; `secret: null` clears both
    `secret_encrypted`/`secret_key_version`; a non-null `secret` is
    re-encrypted. `url`, if present, is re-validated by the router via
    `validate_competitor_url` (same `UNSAFE_URL` rule as create).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    enabled: bool | None = None
    event_types: list[str] | None = None
    secret: str | None = None

    @field_validator("event_types")
    @classmethod
    def _check_event_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return _validate_event_types(value)


class WebhookEndpointResponse(BaseModel):
    """A `webhook_endpoints` row as returned by the API.

    Every column **except** `secret_encrypted`/`secret_key_version`
    (FR-005) — `has_secret` is a derived boolean the router computes
    from `secret_encrypted is not None`. Always built via `_to_response`
    (explicit field mapping), never `model_validate(orm_obj)`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    url: str
    enabled: bool
    event_types: list[str]
    has_secret: bool
    created_at: datetime
    updated_at: datetime


class WebhookEndpointListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/webhook-endpoints`."""

    model_config = ConfigDict(from_attributes=True)

    items: list[WebhookEndpointResponse]
    next_cursor: str | None


def _to_response(endpoint: WebhookEndpoint) -> WebhookEndpointResponse:
    """Explicit field-by-field mapping (never `model_validate(orm_obj)`) so
    `secret_encrypted`/`secret_key_version` can never leak by attribute-copy
    — mirrors `ProxyProviderResponse._to_response` (SPEC-10)."""
    return WebhookEndpointResponse(
        id=endpoint.id,
        workspace_id=endpoint.workspace_id,
        name=endpoint.name,
        url=endpoint.url,
        enabled=endpoint.enabled,
        event_types=list(endpoint.event_types),
        has_secret=endpoint.secret_encrypted is not None,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )
