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

from app_shared.enums import (
    AlertEventType,
    AlertSeverity,
    AlertStatus,
    AlertType,
    HealthStatus,
    StockStatus,
)


class PriceComparisonResponse(BaseModel):
    """`GET /v1/variants/{variant_id}/price-comparison` — a `variant_price_states` row.

    Also the item shape of the bulk list route
    (`GET /v1/variants/price-comparison`) — it already carries
    `product_variant_id`, so no separate list-item DTO is needed.
    """

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


class PriceComparisonListResponse(BaseModel):
    """`GET /v1/variants/price-comparison` — `{items, next_cursor}` envelope.

    The bulk counterpart of the per-variant route: every
    `variant_price_states` row in the workspace, keyset-paginated over
    `(created_at, id)` like every other list endpoint
    (`contracts/pagination.md`). Lets a client snapshot the whole
    price-state table in `ceil(n / limit)` calls instead of one call per
    variant.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[PriceComparisonResponse]
    next_cursor: str | None


class CompetitorPriceResponse(BaseModel):
    """One competitor's latest known price for a variant.

    Item shape of `GET /v1/variants/{variant_id}/competitor-prices` — a
    `competitor_product_matches` row (`match_id`/`competitor_id`/`url`/
    `health_status`) joined to its `match_current_prices` row
    (`price`/`currency`/`scraped_at`) and its competitor's `name`. The
    price fields are nullable: a match that has never been scraped
    successfully still has a row here (the modal shows it as "no price
    yet"), which is why this is not modelled on `MatchCurrentPrice`
    alone.
    """

    model_config = ConfigDict(from_attributes=True)

    match_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    url: str
    price: Decimal | None
    currency: str | None
    scraped_at: datetime | None
    health_status: HealthStatus
    # 2026-08-09 (PLAN_AMAZON_PRICE_FIX, problem 4): "no price" is not one
    # state. `stock_status = OUT_OF_STOCK` with `success = False` means the
    # competitor page says the product is unavailable — the plugin renders
    # an "unavailable" badge (keeping `price`, the last known one, beside
    # it) instead of the blank it used to show for every priceless row.
    # Both are None when the match has no `match_current_prices` row at all
    # (never scraped), which stays distinguishable from a scraped failure.
    stock_status: StockStatus | None = None
    success: bool | None = None


class CompetitorPriceListResponse(BaseModel):
    """`GET /v1/variants/{variant_id}/competitor-prices` — `{items, next_cursor}` envelope.

    Every list route in this API returns the same envelope
    (`contracts/pagination.md`), never a bare JSON array: the per-competitor
    breakdown is keyset-paginated over its `competitor_product_matches`
    rows' `(created_at, id)` like `GET /v1/variants/price-comparison` and
    `GET /v1/alerts/current`.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[CompetitorPriceResponse]
    next_cursor: str | None


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
