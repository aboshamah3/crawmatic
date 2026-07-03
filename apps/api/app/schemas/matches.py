"""Match API DTOs (`contracts/api-matches.md`) — SPEC-05 US2 (+ US3 bulk, later).

Pydantic v2 request/response models for the `/v1/matches` router
(``apps/api/app/routers/matches.py``). Kept in ``apps/api`` (never
``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.competitors``/``catalog``.

Fields deliberately **not** accepted from the client (FR-004/FR-006/
FR-017, `contracts/api-matches.md`): ``product_id`` (derived server-side
from the resolved variant's parent), ``normalized_competitor_url``/
``url_pattern``/``url_pattern_version`` (server-derived by
``app_shared.url_pattern.derive_match_url_fields``), and every health
field (``health_status``/``last_error_code``/``consecutive_failures``/
``success_rate_7d``/``current_price_id``/``last_scraped_at``/
``last_success_at``/``last_failed_at`` — owned by SPEC-07+, defaulted by
the ORM column defaults).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app_shared.enums import HealthStatus, MatchPriority, MatchStatus

# Reused, not rebuilt (contracts/api-matches.md) — the DELETE response
# shape is identical to the SPEC-04 catalog / SPEC-05 competitor delete
# outcome.
from app.schemas.catalog import DeleteOutcome  # noqa: F401 - re-exported for routers


class MatchCreate(BaseModel):
    """`POST /v1/matches` request body.

    The variant is named by exactly one of `product_variant_id` /
    `variant_external_id` / `variant_sku` (resolved in-workspace by the
    router); `competitor_id` + `competitor_url` are always required.
    """

    model_config = ConfigDict(extra="forbid")

    product_variant_id: uuid.UUID | None = None
    variant_external_id: str | None = None
    variant_sku: str | None = None

    competitor_id: uuid.UUID
    competitor_url: str

    competitor_variant_identifier: str | None = None
    competitor_variant_sku: str | None = None
    competitor_variant_options: dict[str, Any] | None = None
    external_title: str | None = None
    scrape_profile_id: uuid.UUID | None = None
    access_policy_id: uuid.UUID | None = None
    priority: MatchPriority = MatchPriority.NORMAL
    status: MatchStatus = MatchStatus.ACTIVE

    @model_validator(mode="after")
    def _check_exactly_one_variant_ref(self) -> "MatchCreate":
        supplied = [
            self.product_variant_id is not None,
            bool(self.variant_external_id),
            bool(self.variant_sku),
        ]
        if sum(supplied) != 1:
            raise ValueError(
                "exactly one of product_variant_id/variant_external_id/"
                "variant_sku must be supplied"
            )
        return self


class MatchUpdate(BaseModel):
    """`PATCH /v1/matches/{id}` — every mutable field optional (partial update).

    The variant/competitor/product identity of a match is immutable via
    PATCH (create a new match instead); if `competitor_url` changes the
    router re-validates + re-derives `normalized_competitor_url`/
    `url_pattern`/`url_pattern_version`. Health fields remain
    server-owned and are never accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    competitor_url: str | None = None
    competitor_variant_identifier: str | None = None
    competitor_variant_sku: str | None = None
    competitor_variant_options: dict[str, Any] | None = None
    external_title: str | None = None
    scrape_profile_id: uuid.UUID | None = None
    access_policy_id: uuid.UUID | None = None
    priority: MatchPriority | None = None
    status: MatchStatus | None = None


class MatchResponse(BaseModel):
    """A `competitor_product_matches` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_url: str
    normalized_competitor_url: str
    url_pattern: str
    url_pattern_version: int
    competitor_variant_identifier: str | None
    competitor_variant_sku: str | None
    competitor_variant_options: dict[str, Any] | None
    external_title: str | None
    scrape_profile_id: uuid.UUID | None
    access_policy_id: uuid.UUID | None
    priority: MatchPriority
    status: MatchStatus
    health_status: HealthStatus
    last_error_code: str | None
    consecutive_failures: int
    success_rate_7d: Decimal | None
    current_price_id: uuid.UUID | None
    last_scraped_at: datetime | None
    last_success_at: datetime | None
    last_failed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatchListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/matches`."""

    items: list[MatchResponse]
    next_cursor: str | None


# --- Bulk-upsert DTOs (US3, `contracts/matches-bulk-upsert.md`) -------------


class MatchBulkUpsertItem(BaseModel):
    """One record in `POST /v1/matches/bulk-upsert`.

    Same field shape as `MatchCreate` — the variant is named by exactly
    one of `product_variant_id`/`variant_external_id`/`variant_sku`
    (resolved in-workspace by the router, one scoped lookup for the
    whole batch); `competitor_id` + `competitor_url` are always
    required. `competitor_url` is safety-validated + normalized per row
    (FR-009/FR-013) — an unsafe URL is reported in the response's
    `rejected[]`, not raised as an error, so it never aborts the rest of
    the batch. Health fields are never client-settable (FR-017).
    """

    model_config = ConfigDict(extra="forbid")

    product_variant_id: uuid.UUID | None = None
    variant_external_id: str | None = None
    variant_sku: str | None = None

    competitor_id: uuid.UUID
    competitor_url: str

    competitor_variant_identifier: str | None = None
    competitor_variant_sku: str | None = None
    competitor_variant_options: dict[str, Any] | None = None
    external_title: str | None = None
    scrape_profile_id: uuid.UUID | None = None
    access_policy_id: uuid.UUID | None = None
    priority: MatchPriority = MatchPriority.NORMAL
    status: MatchStatus = MatchStatus.ACTIVE

    @model_validator(mode="after")
    def _check_exactly_one_variant_ref(self) -> "MatchBulkUpsertItem":
        supplied = [
            self.product_variant_id is not None,
            bool(self.variant_external_id),
            bool(self.variant_sku),
        ]
        if sum(supplied) != 1:
            raise ValueError(
                "exactly one of product_variant_id/variant_external_id/"
                "variant_sku must be supplied"
            )
        return self


class MatchBulkUpsertRequest(BaseModel):
    """`POST /v1/matches/bulk-upsert` request body."""

    model_config = ConfigDict(extra="forbid")

    matches: list[MatchBulkUpsertItem]


class MatchRejectedItem(BaseModel):
    """One entry in `MatchBulkUpsertResult.rejected` (FR-013 reject-and-report)."""

    index: int
    code: str
    reason: str
    url: str


class MatchBulkUpsertResult(BaseModel):
    """`POST /v1/matches/bulk-upsert` response.

    `upserted` counts the safe rows actually written by the single
    set-based `ON CONFLICT ... DO UPDATE` statement (SC-006); `rejected`
    reports every unsafe-URL row by original batch index, never aborting
    the rest of the batch (FR-013).
    """

    upserted: int
    matches: list[MatchResponse]
    rejected: list[MatchRejectedItem]
