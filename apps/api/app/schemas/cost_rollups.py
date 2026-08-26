"""Tenant cost-rollup API DTOs (EPA C6) — `apps/api/app/routers/cost_rollups.py`.

Pydantic v2 response models for `GET /v1/cost-rollups`, the
`cost_rollups:read`-gated, keyset-paginated read over
`network_cost_rollups` (this workspace's own bounded, top-N + "other"
daily cost buckets — see `app_shared.models.network_cost_rollups`'s
module docstring for the ownership/cardinality contract this mirrors).

Kept in `apps/api` (never `app_shared`), the same discipline
`app.schemas.alerts`/`matches`/`catalog`/`jobs` already follow, so the
framework-agnostic core never depends on Pydantic. Money is exchanged as
scaled-integer minor units (`int`, never `Decimal`/`float`) — this is a
cost-ledger-derived figure, not a `Money`-parsed decimal amount, and the
§19 no-float discipline holds regardless of which numeric type a given
surface happens to use.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class CostRollupResponse(BaseModel):
    """One `network_cost_rollups` row — a bounded (workspace, day,
    domain, method, profile_version) bucket.

    `method` is the operation's TRANSPORT (`DIRECT`/`PROXY`/`BROWSER`),
    and `profile_version` is the domain playbook's own version -- see
    `app_shared.models.network_cost_rollups` for why neither is the HTTP
    verb nor the per-decision budget tag.

    `domain`/`method`/`profile_version` read back as the literal
    `"__other__"` sentinel for the collapsed "everything past the top-N"
    bucket (`app_shared.models.network_cost_rollups.
    COST_ROLLUP_OTHER_DOMAIN` etc.) — never a real hostname, since `_`
    cannot appear in a DNS-legal one.

    `reconciled_cost_minor_units` is `None` iff not one operation in this
    bucket has a provider settlement yet — distinct from `0`, which would
    falsely claim "reconciled to zero cost".
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rollup_date: date
    domain: str
    method: str
    profile_version: str
    operation_count: int
    estimated_cost_minor_units: int
    reconciled_cost_minor_units: int | None
    currency: str
    created_at: datetime
    updated_at: datetime


class CostRollupListResponse(BaseModel):
    """`GET /v1/cost-rollups` — `{items, next_cursor}` envelope, keyset-
    paginated over `(created_at, id)` like every other list endpoint
    (`contracts/pagination.md`)."""

    model_config = ConfigDict(from_attributes=True)

    items: list[CostRollupResponse]
    next_cursor: str | None


__all__ = ["CostRollupListResponse", "CostRollupResponse"]
