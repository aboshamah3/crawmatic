"""Alerts/price-comparison API DTOs (`contracts/api-alerts.md`) — SPEC-09 US1 + US2.

Pydantic v2 request/response models for the `/v1/variants/{id}/price-
comparison` route (`apps/api/app/routers/variants.py`) and the
`/v1/alerts/current(+/{variant_id})` + `/v1/alert-events` routers
(`apps/api/app/routers/alerts.py`, US2 T025/T026). Kept in `apps/api`
(never `app_shared`) so the framework-agnostic core never depends on
Pydantic — same discipline as `app.schemas.matches`/`catalog`/`jobs`.

Money/benchmark fields are exchanged as `Decimal | None` (repo
convention, same as `app.schemas.matches.MatchResponse.success_rate_7d`)
— nullable when no comparable competitor exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app_shared.enums import AlertEventType, AlertSeverity, AlertStatus, AlertType


class PriceComparisonResponse(BaseModel):
    """`GET /v1/variants/{variant_id}/price-comparison` — a `variant_price_states` row."""

    model_config = ConfigDict(from_attributes=True)

    product_variant_id: uuid.UUID
    client_price: Decimal
    currency: str
    cheapest_competitor_price: Decimal | None
    average_competitor_price: Decimal | None
    highest_competitor_price: Decimal | None
    comparable_competitor_count: int
    alert_type: AlertType
    alert_severity: AlertSeverity
    calculated_at: datetime


# --- US2 (T024) --------------------------------------------------------------


class AlertStateResponse(BaseModel):
    """A `variant_alert_states` row — the current alert for one variant."""

    model_config = ConfigDict(from_attributes=True)

    product_variant_id: uuid.UUID
    type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    client_price: Decimal
    benchmark_price: Decimal | None
    cheapest_competitor_price: Decimal | None
    average_competitor_price: Decimal | None
    message: str
    details: dict[str, Any] | None
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class AlertStateListResponse(BaseModel):
    """`GET /v1/alerts/current` — `{items, next_cursor}` envelope."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AlertStateResponse]
    next_cursor: str | None


class AlertEventResponse(BaseModel):
    """A `price_alert_events` row — one recorded alert transition."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_variant_id: uuid.UUID
    alert_state_id: uuid.UUID
    event_type: AlertEventType
    previous_type: AlertType | None
    new_type: AlertType
    previous_severity: AlertSeverity | None
    new_severity: AlertSeverity
    message: str
    details: dict[str, Any] | None
    created_at: datetime


class AlertEventListResponse(BaseModel):
    """`GET /v1/alert-events` — `{items, next_cursor}` envelope."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AlertEventResponse]
    next_cursor: str | None
