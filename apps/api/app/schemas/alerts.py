"""Alerts/price-comparison API DTOs (`contracts/api-alerts.md`) — SPEC-09 US1 (+ US2, later).

Pydantic v2 request/response models for the `/v1/variants/{id}/price-
comparison` route (`apps/api/app/routers/variants.py`) and, in US2, the
`/v1/alerts/current(+/{variant_id})` + `/v1/alert-events` routers
(`apps/api/app/routers/alerts.py`). Kept in `apps/api` (never
`app_shared`) so the framework-agnostic core never depends on Pydantic —
same discipline as `app.schemas.matches`/`catalog`/`jobs`.

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

from app_shared.enums import AlertSeverity, AlertType


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


# --- US2 (added in T024) ----------------------------------------------------
# AlertStateResponse / AlertStateListResponse / AlertEventResponse /
# AlertEventListResponse land with `routers/alerts.py` (T025/T026).
