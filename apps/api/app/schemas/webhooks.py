"""Webhook API DTOs (`contracts/rest-api.md`) — SPEC-16.

Pydantic v2 request/response models for the `/v1/webhook-events*` (US1,
T014) and `/v1/webhook-endpoints*` (US2, later phase) routers
(`apps/api/app/routers/webhooks.py`). Kept in `apps/api` (never
`app_shared`) so the framework-agnostic core never depends on Pydantic
— same discipline as `app.schemas.alerts`/`matches`/`catalog`.

`WebhookEventResponse.delivered_at` is always `null` in v1 (FR-010,
SC-007) — the column exists on the ORM model for the future delivery
feature but no write path in this spec ever sets it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


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
