"""Job-creation service (`contracts/job-service.md`, FR-006, FR-010).

Pure-ish orchestration — SQLAlchemy + the `app_shared.messaging` enqueue
seam only (no scrapy/twisted/fastapi). The API router
(`apps/api/app/routers/jobs.py`) resolves the match in-workspace
(`scoped_get`) and delegates job/target creation here, so creation logic
is unit-testable against a fake session + fake `enqueue`.

This US1 slice ships `create_match_job` only; `create_variant_job`
(scope=VARIANT, active-match fan-out, zero-active -> immediate
COMPLETED) is added by US2 (T032) in the same module.

Every read/write is workspace-scoped; the session already carries RLS
context set by the caller (the router's auth seam). Counters start at 0
and are only ever set by `app_shared.jobs.targets.aggregate_counts` —
this module never increments them. This module does not call Scrapyd —
it only creates rows and enqueues the dispatch task.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.messaging import enqueue
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

__all__ = ["create_match_job"]


def _enqueue_dispatch(job_id: uuid.UUID, workspace_id: uuid.UUID | str) -> None:
    enqueue(
        SCRAPE_DISPATCH_JOB,
        queue="scrape_dispatch",
        kwargs={"scrape_job_id": str(job_id), "workspace_id": str(workspace_id)},
    )


def create_match_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    match: CompetitorProductMatch,
    requested_by: uuid.UUID | None,
) -> tuple[uuid.UUID, ScrapeJobStatus]:
    """Create a `scope=MATCH` job with exactly one target, then enqueue dispatch.

    Precondition: `match` is already resolved in-workspace by the
    router (`scoped_get`). Returns `(job.id, ScrapeJobStatus.PENDING)`.
    """
    now = datetime.now(timezone.utc)

    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        product_id=match.product_id,
        product_variant_id=match.product_variant_id,
        competitor_id=match.competitor_id,
        match_id=match.id,
        status=ScrapeJobStatus.PENDING,
        total_targets=1,
        requested_by=requested_by,
        source=ScrapeJobSource.API,
        created_at=now,
    )
    session.add(job)
    session.flush()

    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=match.id,
        status=ScrapeTargetStatus.PENDING,
        created_at=now,
    )
    session.add(target)
    session.flush()

    _enqueue_dispatch(job.id, workspace_id)

    return job.id, ScrapeJobStatus.PENDING
