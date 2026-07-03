"""Competitor API DTOs (`contracts/api-competitors.md`) — SPEC-05 US1.

Pydantic v2 request/response models for the `/v1/competitors` router
(``apps/api/app/routers/competitors.py``). Kept in ``apps/api`` (never
``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.catalog``.

Server-side defaults (``status=ACTIVE``, ``legal_status=REVIEW_REQUIRED``,
``robots_policy=RESPECT``) mirror the ORM column defaults
(``app_shared.models.competitors_matches.Competitor``) exactly, so a
create payload that omits them stores the same defaults whether the row
is built from the schema or the bare model (FR-003, data-model.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app_shared.enums import CompetitorStatus, LegalStatus, RobotsPolicy

# Reused, not rebuilt (contracts/api-competitors.md) — the DELETE
# response shape is identical to the SPEC-04 catalog delete outcome.
from app.schemas.catalog import DeleteOutcome  # noqa: F401 - re-exported for routers


class CompetitorCreate(BaseModel):
    """`POST /v1/competitors` request body.

    `name`/`domain` are required; every other field is optional and
    falls back to the ORM column default when omitted (`status=ACTIVE`,
    `legal_status=REVIEW_REQUIRED`, `robots_policy=RESPECT`).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    domain: str
    status: CompetitorStatus = CompetitorStatus.ACTIVE
    legal_status: LegalStatus = LegalStatus.REVIEW_REQUIRED
    robots_policy: RobotsPolicy = RobotsPolicy.RESPECT
    default_scrape_profile_id: uuid.UUID | None = None
    default_access_policy_id: uuid.UUID | None = None
    max_concurrent_requests: int | None = None
    max_requests_per_minute: int | None = None


class CompetitorUpdate(BaseModel):
    """`PATCH /v1/competitors/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    status: CompetitorStatus | None = None
    legal_status: LegalStatus | None = None
    robots_policy: RobotsPolicy | None = None
    default_scrape_profile_id: uuid.UUID | None = None
    default_access_policy_id: uuid.UUID | None = None
    max_concurrent_requests: int | None = None
    max_requests_per_minute: int | None = None


class CompetitorResponse(BaseModel):
    """A `competitors` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    domain: str
    status: CompetitorStatus
    legal_status: LegalStatus
    robots_policy: RobotsPolicy
    default_scrape_profile_id: uuid.UUID | None
    default_access_policy_id: uuid.UUID | None
    max_concurrent_requests: int | None
    max_requests_per_minute: int | None
    created_at: datetime
    updated_at: datetime


class CompetitorListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/competitors`."""

    items: list[CompetitorResponse]
    next_cursor: str | None
