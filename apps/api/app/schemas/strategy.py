"""Domain strategy optimizer API DTOs (`contracts/api-and-observability.md`) — SPEC-12 US3.

Pydantic v2 request/response models for the `/v1/strategy` router
(`apps/api/app/routers/strategy.py`). Kept in `apps/api` (never
`app_shared`) so the framework-agnostic core never depends on Pydantic —
same discipline as `app.schemas.jobs`/`access`.

`Decimal` confidence fields serialize as strings by default (Pydantic v2
`model_dump(mode="json")`/`model_dump_json()`, Constitution VII — no
float) — no custom encoder needed, same as `app.schemas.alerts`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from app_shared.enums import (
    AccessMethod,
    DiscoveryRunStatus,
    ExtractionMethod,
    MethodType,
    StrategyStatus,
)
from app_shared.strategy.seed import validate_sample_size

# Mirrors `Settings.STRATEGY_DISCOVERY_MIN_SAMPLE`/`_MAX_SAMPLE`'s documented
# defaults (data-model.md §7). Deliberately NOT read via `get_settings()`
# here: `Settings` is a `BaseSettings` requiring a fully configured process
# environment (`DATABASE_URL`/`REDIS_URL`/...) to construct at all, which
# would make every request body -- even one FastAPI rejects before any
# handler/session runs -- depend on live process config; no other schema in
# `apps/api/app/schemas/` does this. This is a fast, static defense-in-depth
# 422 using the shipped default bound; `apps/workers/app/workers
# /tasks_strategy.py::run_discovery` is the actual live-`Settings`-honoring
# enforcement point (contracts/discovery.md step 1) an operator's env-var
# override of the knob actually governs.
_DEFAULT_MIN_SAMPLE = 3
_DEFAULT_MAX_SAMPLE = 10


class DiscoveryRunCreate(BaseModel):
    """`POST /v1/strategy/discovery-runs` request body (FR-016/FR-019, US3 AS2)."""

    competitor_id: uuid.UUID
    domain: str
    url_pattern: str
    sample_urls: list[str]

    @field_validator("sample_urls")
    @classmethod
    def _check_sample_size(cls, value: list[str]) -> list[str]:
        if not validate_sample_size(
            len(value), min_sample=_DEFAULT_MIN_SAMPLE, max_sample=_DEFAULT_MAX_SAMPLE
        ):
            raise ValueError(
                f"sample_urls must contain between {_DEFAULT_MIN_SAMPLE} "
                f"and {_DEFAULT_MAX_SAMPLE} URLs (got {len(value)})"
            )
        return value


class DiscoveryRunResponse(BaseModel):
    """One `strategy_discovery_runs` row, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    competitor_id: uuid.UUID
    domain: str
    url_pattern: str
    sample_size: int
    status: DiscoveryRunStatus
    winning_access_method: AccessMethod | None
    winning_extraction_method: ExtractionMethod | None
    created_at: datetime
    completed_at: datetime | None


class DiscoveryRunListResponse(BaseModel):
    """`GET /v1/strategy/discovery-runs` cursor-paginated envelope."""

    items: list[DiscoveryRunResponse]
    next_cursor: str | None = None


# --- Learned profiles (GET /v1/strategy/profiles[/{id}], PATCH .../{id}) ---
# SPEC-12 Polish T039. `Decimal` confidence/rate fields serialize as strings
# (Constitution VII), same discipline as the discovery DTOs above.


class StrategyMethodStatsResponse(BaseModel):
    """One `strategy_attempt_stats` row (per-method rolling stats)."""

    model_config = ConfigDict(from_attributes=True)

    method_type: MethodType
    method_name: str
    attempt_count: int
    success_count: int
    failure_count: int
    success_rate: Decimal
    avg_response_time_ms: int | None
    avg_confidence: Decimal | None
    last_success_at: datetime | None
    last_failed_at: datetime | None


class StrategyProfileResponse(BaseModel):
    """One `domain_strategy_profiles` row, as returned by the list/get API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    competitor_id: uuid.UUID
    domain: str
    url_pattern: str
    url_pattern_version: int
    status: StrategyStatus
    preferred_access_method: AccessMethod | None
    preferred_extraction_method: ExtractionMethod | None
    access_confidence: Decimal | None
    extraction_confidence: Decimal | None
    confirmed_success_count: int
    recent_failure_count: int
    last_discovery_at: datetime | None
    last_success_at: datetime | None
    last_failed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StrategyProfileDetailResponse(StrategyProfileResponse):
    """A single profile plus its per-method stats (`GET .../profiles/{id}`)."""

    stats: list[StrategyMethodStatsResponse] = []


class StrategyProfileListResponse(BaseModel):
    """`GET /v1/strategy/profiles` cursor-paginated envelope."""

    items: list[StrategyProfileResponse]
    next_cursor: str | None = None


class StrategyProfileUpdate(BaseModel):
    """`PATCH /v1/strategy/profiles/{id}` operator override body (FR-006/FR-014).

    All fields optional; at least one must be provided. `url_pattern` sets a
    manual pattern override; `status` may be set to `DISABLED` (stop applying
    the learned preference) or back to an active state to re-enable.
    """

    url_pattern: str | None = None
    status: StrategyStatus | None = None

    @field_validator("url_pattern")
    @classmethod
    def _non_empty_pattern(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("url_pattern override must be non-empty")
        return value
