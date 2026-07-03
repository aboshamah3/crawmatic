"""Scrape-profile API DTOs (`contracts/api-scrape-profiles.md`) — SPEC-06 US1.

Pydantic v2 request/response models for the `/v1/scrape-profiles` router
(``apps/api/app/routers/scrape_profiles.py``). Kept in ``apps/api``
(never ``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.competitors``/``matches``.

``workspace_id`` is **never** client-supplied on create/update — the
router always stamps the caller's own workspace (a tenant can never
write a global profile through the API, FR-021). Server-side defaults
(``mode=HTTP``, ``adapter_key=default_http``, the three ``*_enabled``
flags, ``variant_strategy=PAGE_SINGLE_PRICE``,
``request_timeout_ms=30000``) mirror the ORM column defaults exactly
(``app_shared.models.scrape_profiles.ScrapeProfile``, FR-002).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app_shared.enums import AdapterKey, ScrapeProfileMode, VariantStrategy

# Reused, not rebuilt (contracts/api-scrape-profiles.md) — the DELETE
# response shape is identical to the SPEC-04 catalog / SPEC-05
# competitor/match delete outcome.
from app.schemas.catalog import DeleteOutcome  # noqa: F401 - re-exported for routers


class ScrapeProfileCreate(BaseModel):
    """`POST /v1/scrape-profiles` request body.

    `name` is required; every other field falls back to the ORM column
    default when omitted. `workspace_id` is never accepted here — the
    router always stores the caller's own workspace (never global).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    mode: ScrapeProfileMode = ScrapeProfileMode.HTTP
    adapter_key: AdapterKey = AdapterKey.DEFAULT_HTTP
    jsonld_enabled: bool = True
    platform_patterns_enabled: bool = True
    embedded_json_enabled: bool = True

    price_selector: str | None = None
    price_xpath: str | None = None
    price_regex: str | None = None
    old_price_selector: str | None = None
    old_price_xpath: str | None = None
    old_price_regex: str | None = None
    currency_selector: str | None = None
    currency_xpath: str | None = None
    currency_regex: str | None = None
    stock_selector: str | None = None
    stock_xpath: str | None = None
    stock_regex: str | None = None
    title_selector: str | None = None
    title_xpath: str | None = None

    variant_strategy: VariantStrategy = VariantStrategy.PAGE_SINGLE_PRICE
    variant_selector_config: dict[str, Any] | None = None
    price_transform_rules: dict[str, Any] | None = None
    validation_rules: dict[str, Any] | None = None
    confidence_rules: dict[str, Any] | None = None

    wait_for_selector: str | None = None
    request_timeout_ms: int = 30000
    browser_timeout_ms: int | None = None
    headers: dict[str, Any] | None = None
    cookies: dict[str, Any] | list[dict[str, Any]] | None = None


class ScrapeProfileUpdate(BaseModel):
    """`PATCH /v1/scrape-profiles/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    mode: ScrapeProfileMode | None = None
    adapter_key: AdapterKey | None = None
    jsonld_enabled: bool | None = None
    platform_patterns_enabled: bool | None = None
    embedded_json_enabled: bool | None = None

    price_selector: str | None = None
    price_xpath: str | None = None
    price_regex: str | None = None
    old_price_selector: str | None = None
    old_price_xpath: str | None = None
    old_price_regex: str | None = None
    currency_selector: str | None = None
    currency_xpath: str | None = None
    currency_regex: str | None = None
    stock_selector: str | None = None
    stock_xpath: str | None = None
    stock_regex: str | None = None
    title_selector: str | None = None
    title_xpath: str | None = None

    variant_strategy: VariantStrategy | None = None
    variant_selector_config: dict[str, Any] | None = None
    price_transform_rules: dict[str, Any] | None = None
    validation_rules: dict[str, Any] | None = None
    confidence_rules: dict[str, Any] | None = None

    wait_for_selector: str | None = None
    request_timeout_ms: int | None = None
    browser_timeout_ms: int | None = None
    headers: dict[str, Any] | None = None
    cookies: dict[str, Any] | list[dict[str, Any]] | None = None


class ScrapeProfileResponse(BaseModel):
    """A `scrape_profiles` row as returned by the API.

    `workspace_id` is `null` for a global (shared) profile — surfaced
    verbatim so a caller can distinguish its own rows from globals.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID | None
    name: str
    mode: ScrapeProfileMode
    adapter_key: AdapterKey
    jsonld_enabled: bool
    platform_patterns_enabled: bool
    embedded_json_enabled: bool

    price_selector: str | None
    price_xpath: str | None
    price_regex: str | None
    old_price_selector: str | None
    old_price_xpath: str | None
    old_price_regex: str | None
    currency_selector: str | None
    currency_xpath: str | None
    currency_regex: str | None
    stock_selector: str | None
    stock_xpath: str | None
    stock_regex: str | None
    title_selector: str | None
    title_xpath: str | None

    variant_strategy: VariantStrategy
    variant_selector_config: dict[str, Any] | None
    price_transform_rules: dict[str, Any] | None
    validation_rules: dict[str, Any] | None
    confidence_rules: dict[str, Any] | None

    wait_for_selector: str | None
    request_timeout_ms: int
    browser_timeout_ms: int | None
    headers: dict[str, Any] | None
    cookies: dict[str, Any] | list[dict[str, Any]] | None

    created_at: datetime
    updated_at: datetime


class ScrapeProfileListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/scrape-profiles`."""

    items: list[ScrapeProfileResponse]
    next_cursor: str | None


# --- Bulk-upsert DTOs (`contracts/profiles-bulk-upsert.md`) -----------------


class ScrapeProfileBulkUpsertItem(BaseModel):
    """One record in `POST /v1/scrape-profiles/bulk-upsert`.

    Same field shape as `ScrapeProfileCreate` — matched on
    `(workspace_id, name)`, `workspace_id` always stamped server-side
    (never client-supplied, never global).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    mode: ScrapeProfileMode = ScrapeProfileMode.HTTP
    adapter_key: AdapterKey = AdapterKey.DEFAULT_HTTP
    jsonld_enabled: bool = True
    platform_patterns_enabled: bool = True
    embedded_json_enabled: bool = True

    price_selector: str | None = None
    price_xpath: str | None = None
    price_regex: str | None = None
    old_price_selector: str | None = None
    old_price_xpath: str | None = None
    old_price_regex: str | None = None
    currency_selector: str | None = None
    currency_xpath: str | None = None
    currency_regex: str | None = None
    stock_selector: str | None = None
    stock_xpath: str | None = None
    stock_regex: str | None = None
    title_selector: str | None = None
    title_xpath: str | None = None

    variant_strategy: VariantStrategy = VariantStrategy.PAGE_SINGLE_PRICE
    variant_selector_config: dict[str, Any] | None = None
    price_transform_rules: dict[str, Any] | None = None
    validation_rules: dict[str, Any] | None = None
    confidence_rules: dict[str, Any] | None = None

    wait_for_selector: str | None = None
    request_timeout_ms: int = 30000
    browser_timeout_ms: int | None = None
    headers: dict[str, Any] | None = None
    cookies: dict[str, Any] | list[dict[str, Any]] | None = None


class ScrapeProfileBulkUpsertRequest(BaseModel):
    """`POST /v1/scrape-profiles/bulk-upsert` request body."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[ScrapeProfileBulkUpsertItem]


class ScrapeProfileRejectedItem(BaseModel):
    """One entry in `ScrapeProfileBulkUpsertResult.rejected` (FR-020 reject-and-report)."""

    index: int
    name: str | None
    field: str
    code: str
    reason: str


class ScrapeProfileBulkUpsertResult(BaseModel):
    """`POST /v1/scrape-profiles/bulk-upsert` response.

    `upserted` counts the valid rows actually written by the single
    set-based `ON CONFLICT ... DO UPDATE` statement (SC-008); `rejected`
    reports every invalid row by original batch index, never aborting
    the rest of the batch (FR-020).
    """

    upserted: int
    profiles: list[ScrapeProfileResponse]
    rejected: list[ScrapeProfileRejectedItem]


# --- Workspace-default assignment (`PUT /v1/scrape-profiles/workspace-default`) --


class WorkspaceDefaultProfileAssignment(BaseModel):
    """`PUT /v1/scrape-profiles/workspace-default` request body.

    `profile_id: null` clears the workspace's default (FR-012/FR-013).
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID | None = None
