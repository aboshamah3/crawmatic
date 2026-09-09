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

from app_shared.enums import ApiKeyStatus, WorkspaceStatus


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
    #: Task B3 (2026-09-03): per (workspace, product, cycle) counts of the
    #: underlying `network_operations` (B2) PROXY/BROWSER transport rows —
    #: retries included, unlike the folded `links_total` counters above.
    #: Additive: default 0 lets an older row shape (no network_operations
    #: match) still validate.
    proxied_http_attempted: int = 0
    proxied_browser_attempted: int = 0
    proxy_bytes: int = 0
    #: Task C6/F17 (2026-09-08). This workspace's OWN share of the
    #: physical operations the cycle used, read from
    #: `network_operation_allocations` — never the whole physical cost of
    #: an operation three co-tenants shared. Additive with a `0` default
    #: for the same reason the three B3 fields above are: a row shape
    #: without it still validates.
    allocated_cost_micro_units: int = 0


class UsageListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/admin/usage`."""

    items: list[UsageRow]
    next_cursor: str | None


class AdminApiKeyCreateRequest(BaseModel):
    """`POST /v1/admin/workspaces/{workspace_id}/api-keys` body.

    `scopes` omitted (or `null`) means "use the default set" --
    `app.routers.admin.BOOTSTRAP_SCOPES`, the same tenant-scope set a
    workspace's own bootstrap key gets at provisioning time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] | None = None


class AdminApiKeyCreateResponse(BaseModel):
    """The plaintext key is returned exactly once and never stored."""

    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: ApiKeyStatus
    created_at: datetime
    api_key: str  # the full secret — shown exactly once


class AdminApiKeyListItem(BaseModel):
    """One row of `GET /v1/admin/workspaces/{workspace_id}/api-keys` --
    never `key_hash`, never plaintext."""

    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: ApiKeyStatus
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class AdminApiKeyListResponse(BaseModel):
    items: list[AdminApiKeyListItem]


class ConnectorKeyCreateRequest(BaseModel):
    """`POST /v1/admin/workspaces/{workspace_id}/connector-keys` body.

    `connector` names the WordPress connector the key is being minted
    for (e.g. `"woo"`) -- it drives the stored key `name`
    (`f"connector:{connector}:{workspace_id}"`) and nothing else; the
    scope set is always `app.routers.admin.CONNECTOR_SCOPES`, never
    caller-supplied (audit P0.3 -- WordPress never holds a key wider
    than read-catalog + manage-competitors/matches).
    """

    model_config = ConfigDict(extra="forbid")

    connector: str = Field(min_length=1, max_length=100)


class ConnectorKeyCreateResponse(BaseModel):
    """The plaintext key is returned exactly once and never stored."""

    key_id: uuid.UUID
    api_key: str
    key_prefix: str


class ApiKeyRevokeResponse(BaseModel):
    """`POST /v1/admin/api-keys/{key_id}/revoke` response."""

    key_id: uuid.UUID
    status: ApiKeyStatus


class TargetReconciliationRequest(BaseModel):
    """Dry-run-first repair request for one explicitly selected scrape job."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    expected_match_ids: list[uuid.UUID] | None = None
    requested_by: str | None = Field(default=None, min_length=1, max_length=200)


class TargetReconciliationCounts(BaseModel):
    success: int
    failure: int
    skipped: int
    total: int


class TargetReconciliationResponse(BaseModel):
    workspace_id: uuid.UUID
    scrape_job_id: uuid.UUID
    dry_run: bool
    candidate_match_ids: list[uuid.UUID]
    candidate_count: int
    before: TargetReconciliationCounts
    projected: TargetReconciliationCounts
    job_status_before: str
    job_status_projected: str | None
    applied_count: int


class ProfileRegexUnquarantineResponse(BaseModel):
    """Result of releasing a scrape profile's regex quarantine (A2/F02).

    `was_quarantined` reports the state the call *found*, so an operator can
    tell a real release from a no-op re-run without a second request; both
    are 200 (the endpoint is idempotent).
    """

    scrape_profile_id: uuid.UUID
    was_quarantined: bool
    quarantined_at: datetime | None
    regex_timeout_count: int
