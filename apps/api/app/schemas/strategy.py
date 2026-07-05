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

from pydantic import BaseModel, ConfigDict, field_validator

from app_shared.enums import AccessMethod, DiscoveryRunStatus, ExtractionMethod
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


# `StrategyProfileResponse`/`StrategyProfileListResponse` (GET /v1/strategy/
# profiles[/{id}], PATCH .../{id}) are Polish-phase T039 — out of scope for
# this US3 slice (T024-T028); added there alongside the profile read/manage
# endpoints.
