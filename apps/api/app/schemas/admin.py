"""DTOs for the SaaS admin surface (`/v1/admin/*`, PLAN §7.1–§7.2).

Lives in `apps/api` like every other schema module — `app_shared` must
never import pydantic.

The usage-export field names are a **frozen contract**: the SaaS
metering consumer keys `UsageSnapshot` on
`(workspace_id, product_id, cycle_ts)` and prices from the counters.
Renaming any field here breaks billing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app_shared.enums import WorkspaceStatus


class WorkspaceProvisionRequest(BaseModel):
    """`POST /v1/admin/workspaces` body."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    external_ref: str = Field(min_length=1, max_length=200)


class WorkspaceProvisionResponse(BaseModel):
    """The one and only time the bootstrap key is returned in plaintext."""

    workspace_id: uuid.UUID
    api_key: str
    external_ref: str


class WorkspaceArchiveResponse(BaseModel):
    """`WorkspaceStatus`, not bare `str` (review finding I6d) -- a future
    change away from `StrEnum` cannot silently start returning
    `"WorkspaceStatus.SUSPENDED"` through this field undetected."""

    workspace_id: uuid.UUID
    status: WorkspaceStatus


class UsageRow(BaseModel):
    """One product-check cycle. **Field names are contractual.**"""

    model_config = ConfigDict(from_attributes=True)

    workspace_id: uuid.UUID
    product_id: uuid.UUID
    cycle_ts: datetime
    links_total: int
    links_succeeded: int
    protected_links_attempted: int
    protected_links_succeeded: int
    check_successful: bool


class UsageListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/admin/usage`."""

    items: list[UsageRow]
    next_cursor: str | None
