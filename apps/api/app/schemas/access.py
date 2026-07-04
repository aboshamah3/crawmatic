"""Access-config API DTOs (`contracts/api-access.md`) — SPEC-10 US1.

Pydantic v2 request/response models for the three `/v1` access-config
routers (`proxy_providers`, `access_policies`, `domain_access_rules`).
Kept in `apps/api` (never `app_shared`) so the framework-agnostic core
never depends on Pydantic — same discipline as
`app.schemas.competitors`/`app.schemas.scrape_profiles`.

Password handling (SC-003): `ProxyProviderCreate`/`Update` accept a
plaintext `password` field that is **never** persisted on the ORM as-is
(the router encrypts it via `app_shared.security.encryption.encrypt_secret`
into `password_encrypted`/`password_key_version`). `ProxyProviderResponse`
carries every other column but **never** `password_encrypted`/
`password_key_version` — only a derived boolean `has_password`, so no
response can ever leak plaintext or ciphertext.

`workspace_id` is never client-supplied on create for any of the three
tables — the router always stamps the caller's own workspace (a tenant
can never write a global provider/policy through the API, FR-006).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app_shared.enums import AccessStrategy, ProxyProviderStatus, ProxyType

# Reused, not rebuilt (contracts/api-access.md) — the DELETE response
# shape is identical to the SPEC-04 catalog / SPEC-05/06 delete outcome.
from app.schemas.catalog import DeleteOutcome  # noqa: F401 - re-exported for routers


# --- ProxyProvider -----------------------------------------------------


class ProxyProviderCreate(BaseModel):
    """`POST /v1/proxy-providers` request body.

    `password`, if supplied, is plaintext on the wire only — the router
    encrypts it before storage and never echoes it back (SC-003).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ProxyType
    base_url: str
    username: str | None = None
    password: str | None = None
    country_code: str | None = None
    status: ProxyProviderStatus = ProxyProviderStatus.ACTIVE
    monthly_budget_limit: int | None = Field(default=None, gt=0)


class ProxyProviderUpdate(BaseModel):
    """`PATCH /v1/proxy-providers/{id}` — every field optional (partial update).

    Distinguishes "omitted" (unchanged) from "explicitly null" via the
    router's `exclude_unset=True` dump: an omitted `password` leaves the
    stored ciphertext untouched; `password: null` clears both
    `password_encrypted`/`password_key_version`; a non-null `password`
    is re-encrypted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    type: ProxyType | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    country_code: str | None = None
    status: ProxyProviderStatus | None = None
    monthly_budget_limit: int | None = Field(default=None, gt=0)


class ProxyProviderResponse(BaseModel):
    """A `proxy_providers` row as returned by the API.

    Every column **except** `password_encrypted`/`password_key_version`
    (SC-003) — `has_password` is a derived boolean the router computes
    from `password_encrypted is not None`. `workspace_id` is `null` for
    a global (shared) provider.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID | None
    name: str
    type: ProxyType
    base_url: str
    username: str | None
    has_password: bool
    country_code: str | None
    status: ProxyProviderStatus
    monthly_budget_limit: int | None
    created_at: datetime
    updated_at: datetime


class ProxyProviderListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/proxy-providers`."""

    items: list[ProxyProviderResponse]
    next_cursor: str | None


# --- AccessPolicy --------------------------------------------------------


class AccessPolicyCreate(BaseModel):
    """`POST /v1/access-policies` request body (full FR-001 field set)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    strategy: AccessStrategy
    provider_id: uuid.UUID | None = None
    country_code: str | None = None
    use_proxy_on_first_attempt: bool = False
    use_proxy_on_retry: bool = True
    allow_browser_fallback: bool = False
    max_retries: int = Field(default=2, ge=0)
    rotate_per_request: bool = False
    sticky_session: bool = False
    session_ttl_minutes: int | None = None
    max_requests_per_minute: int | None = Field(default=None, gt=0)
    max_requests_per_hour: int | None = Field(default=None, gt=0)
    max_requests_per_day: int | None = Field(default=None, gt=0)
    timeout_ms: int = Field(default=30000, gt=0)


class AccessPolicyUpdate(BaseModel):
    """`PATCH /v1/access-policies/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    strategy: AccessStrategy | None = None
    provider_id: uuid.UUID | None = None
    country_code: str | None = None
    use_proxy_on_first_attempt: bool | None = None
    use_proxy_on_retry: bool | None = None
    allow_browser_fallback: bool | None = None
    max_retries: int | None = Field(default=None, ge=0)
    rotate_per_request: bool | None = None
    sticky_session: bool | None = None
    session_ttl_minutes: int | None = None
    max_requests_per_minute: int | None = Field(default=None, gt=0)
    max_requests_per_hour: int | None = Field(default=None, gt=0)
    max_requests_per_day: int | None = Field(default=None, gt=0)
    timeout_ms: int | None = Field(default=None, gt=0)


class AccessPolicyResponse(BaseModel):
    """An `access_policies` row as returned by the API.

    `workspace_id` is `null` for a global (shared) policy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID | None
    name: str
    strategy: AccessStrategy
    provider_id: uuid.UUID | None
    country_code: str | None
    use_proxy_on_first_attempt: bool
    use_proxy_on_retry: bool
    allow_browser_fallback: bool
    max_retries: int
    rotate_per_request: bool
    sticky_session: bool
    session_ttl_minutes: int | None
    max_requests_per_minute: int | None
    max_requests_per_hour: int | None
    max_requests_per_day: int | None
    timeout_ms: int
    created_at: datetime
    updated_at: datetime


class AccessPolicyListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/access-policies`."""

    items: list[AccessPolicyResponse]
    next_cursor: str | None


# --- DomainAccessRule -----------------------------------------------------


class DomainAccessRuleCreate(BaseModel):
    """`POST /v1/domain-access-rules` request body (full FR-004 field set).

    Tenant-only — `workspace_id` is never client-supplied; the router
    always stamps the caller's own workspace.
    """

    model_config = ConfigDict(extra="forbid")

    competitor_id: uuid.UUID
    domain: str
    url_pattern: str | None = None
    url_pattern_override: str | None = None
    access_policy_id: uuid.UUID
    max_concurrent_requests: int = Field(ge=1)
    max_requests_per_minute: int = Field(ge=0)
    cooldown_seconds: int = Field(ge=0)
    block_detection_rules: dict[str, Any] | None = None
    enabled: bool = True


class DomainAccessRuleUpdate(BaseModel):
    """`PATCH /v1/domain-access-rules/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    competitor_id: uuid.UUID | None = None
    domain: str | None = None
    url_pattern: str | None = None
    url_pattern_override: str | None = None
    access_policy_id: uuid.UUID | None = None
    max_concurrent_requests: int | None = Field(default=None, ge=1)
    max_requests_per_minute: int | None = Field(default=None, ge=0)
    cooldown_seconds: int | None = Field(default=None, ge=0)
    block_detection_rules: dict[str, Any] | None = None
    enabled: bool | None = None


class DomainAccessRuleResponse(BaseModel):
    """A `domain_access_rules` row as returned by the API (tenant-only)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    competitor_id: uuid.UUID
    domain: str
    url_pattern: str | None
    url_pattern_override: str | None
    access_policy_id: uuid.UUID
    max_concurrent_requests: int
    max_requests_per_minute: int
    cooldown_seconds: int
    block_detection_rules: dict[str, Any] | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class DomainAccessRuleListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/domain-access-rules`."""

    items: list[DomainAccessRuleResponse]
    next_cursor: str | None
