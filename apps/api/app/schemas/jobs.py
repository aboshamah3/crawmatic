"""Jobs API DTOs (`contracts/api-jobs.md`) — SPEC-08 US1 (run-match/get/results).

Pydantic v2 request/response models for the `/v1/jobs` router
(``apps/api/app/routers/jobs.py``) — plus :class:`VariantRescrapeResponse`,
the job-shaped reply of the plugin's on-demand
``POST /v1/variants/{variant_id}/rescrape`` (which lives on the variants
router but answers with a job id, so its DTO belongs here next to
``JobResponse`` rather than in ``app.schemas.catalog``). Kept in
``apps/api`` (never
``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.matches``/``competitors``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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


class VariantRescrapeResponse(BaseModel):
    """`POST /v1/variants/{variant_id}/rescrape` response (202).

    Deliberately *not* `JobRunResponse`: the plugin's on-demand refresh
    only ever produces a `PENDING` job (a zero-active-match variant is a
    `409 NO_ACTIVE_MATCHES`, never a job), so `status` would be a
    constant — what the caller actually needs back is the id to poll
    (`GET /v1/jobs/{job_id}`) and how many competitor pages it is waiting
    on.
    """

    job_id: uuid.UUID
    match_count: int


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
    """One `scrape_job_targets` row, as returned by `GET /v1/jobs/{id}/results`.

    `status` is the `ScrapeTargetStatus` vocabulary verbatim, so the
    EPA A2 `CANCELLED` member surfaces here automatically. The three
    cancellation columns are published alongside it: a client that sees
    a target finish with no result must be able to tell "a human closed
    this, here is who and why" from "the scraper gave up", and the status
    string alone cannot carry that. All three are `None` for every target
    that was never cancelled.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_id: uuid.UUID
    status: ScrapeTargetStatus
    error_code: ScrapeErrorCode | None
    started_at: datetime | None
    completed_at: datetime | None
    locked_at: datetime | None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancelled_reason: str | None = None


class JobResultsResponse(BaseModel):
    """`GET /v1/jobs/{job_id}/results` response envelope."""

    items: list[JobTargetResponse]
    next_cursor: str | None = None


class JobCancelRequest(BaseModel):
    """`POST /v1/admin/jobs/{job_id}/cancel` request body (EPA A2).

    Carries the *reason* and nothing else. There is deliberately no
    ``actor``/``requested_by`` field: the actor is taken from the
    authenticated principal, so it cannot be typed in — a
    caller-suppliable actor is a signature anyone holding the key can
    forge, which would make the audit trail worth exactly nothing.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Why this job is being closed. Recorded on every cancelled target "
            "row and in the scrape.job.cancelled event. Required: an "
            "unexplained cancellation is not auditable."
        ),
    )


class JobCancelResponse(BaseModel):
    """`POST /v1/admin/jobs/{job_id}/cancel` response — a `CancellationReport`.

    ``targets_cancelled`` counts only the rows *this* call moved, so a
    replay answers ``0`` with ``idempotent_replay=true`` rather than
    restating the job's shape. ``outbox_message_id`` is the durable
    event this call recorded, and is ``null`` on a replay because exactly
    one event exists per job cancellation.
    """

    job_id: uuid.UUID
    targets_cancelled: int
    targets_already_terminal: int
    outbox_message_id: uuid.UUID | None = None
    idempotent_replay: bool
    actor: str
