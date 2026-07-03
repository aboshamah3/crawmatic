"""Jobs API DTOs (`contracts/api-jobs.md`) — SPEC-08 US1 (run-match/get/results).

Pydantic v2 request/response models for the `/v1/jobs` router
(``apps/api/app/routers/jobs.py``). Kept in ``apps/api`` (never
``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.matches``/``competitors``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app_shared.enums import (
    MatchPriority,
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)


class JobRunResponse(BaseModel):
    """`POST /v1/jobs/run/match/{id}` (and `run/variant/{id}`, US2) response."""

    id: uuid.UUID
    status: ScrapeJobStatus


class JobResponse(BaseModel):
    """`GET /v1/jobs/{job_id}` — a `scrape_jobs` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: ScrapeJobType
    scope: ScrapeScope
    status: ScrapeJobStatus
    priority: MatchPriority
    total_targets: int
    success_count: int
    failure_count: int
    skipped_count: int
    requested_by: uuid.UUID | None
    source: ScrapeJobSource
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class JobTargetResponse(BaseModel):
    """One `scrape_job_targets` row, as returned by `GET /v1/jobs/{id}/results`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_id: uuid.UUID
    status: ScrapeTargetStatus
    error_code: ScrapeErrorCode | None
    started_at: datetime | None
    completed_at: datetime | None
    locked_at: datetime | None


class JobResultsResponse(BaseModel):
    """`GET /v1/jobs/{job_id}/results` response envelope."""

    items: list[JobTargetResponse]
