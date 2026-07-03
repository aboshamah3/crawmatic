"""Target-row lifecycle transitions + counter aggregation.

Per ``contracts/lifecycle-counters.md`` (D5, FR-017, FR-018). Pure
SQLAlchemy — no scrapy/twisted/fastapi.

:func:`mark_target` is the **single writer** of a ``scrape_job_targets``
row's ``status``/timestamps/``error_code`` transition
(``PENDING -> STARTED -> COMPLETED/FAILED/SKIPPED``). It touches ONLY the
target row it resolves — never the parent job's counters. This is the
FR-017 seam the SPEC-07 item-persistence pipeline calls as targets
progress (wired in T052; that pipeline lives outside this package so
this module stays scrapy/twisted/fastapi-free and importable from it).

:func:`aggregate_counts` performs the one scoped ``GROUP BY status`` read
the ``finalize_jobs``/``refresh_job_counters`` maintenance tasks
(``apps/workers/app/workers/tasks_jobs.py``) use to overwrite a job row's
counters in a single ``UPDATE`` — never a per-target increment (FR-018,
SC-004, Principle VIII).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.models.jobs import ScrapeJobTarget

__all__ = ["Counts", "aggregate_counts", "mark_target"]

# A target in one of these statuses has reached a terminal outcome —
# `completed_at` is stamped exactly once, on the transition into one of
# these (D5).
_TERMINAL_TARGET_STATUSES = frozenset(
    {
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
    }
)


@dataclass(frozen=True)
class Counts:
    """Aggregated `scrape_job_targets` counts for one job."""

    success: int
    failure: int
    skipped: int
    total: int


def mark_target(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    status: ScrapeTargetStatus,
    error_code: ScrapeErrorCode | None = None,
) -> None:
    """Transition the target ``(workspace_id, scrape_job_id, match_id)``.

    Sets ``started_at`` on a transition to ``STARTED``, ``completed_at``
    on a transition to any terminal status, and ``error_code`` on a
    transition to ``FAILED``. Touches ONLY this target row — never the
    parent job's counters (D5); ``aggregate_counts`` is the sole counter
    source. A no-op if the target can't be resolved in-workspace (e.g. an
    already-deleted/archived row).
    """
    stmt = select(ScrapeJobTarget).where(
        ScrapeJobTarget.workspace_id == workspace_id,
        ScrapeJobTarget.scrape_job_id == scrape_job_id,
        ScrapeJobTarget.match_id == match_id,
    )
    target = session.execute(stmt).scalar_one_or_none()
    if target is None:
        return

    target.status = status
    now = datetime.now(timezone.utc)
    if status == ScrapeTargetStatus.STARTED:
        target.started_at = now
    if status in _TERMINAL_TARGET_STATUSES:
        target.completed_at = now
    if status == ScrapeTargetStatus.FAILED:
        target.error_code = error_code


def aggregate_counts(
    session: Session,
    scrape_job_id: uuid.UUID | str,
    workspace_id: uuid.UUID | str,
) -> Counts:
    """One scoped ``SELECT status, COUNT(*) ... GROUP BY status`` for `scrape_job_id`.

    Callers (``finalize_jobs``/``refresh_job_counters``) write the
    resulting totals to the job row in **one** ``UPDATE`` — never a
    per-target increment (FR-018, SC-004).
    """
    stmt = (
        select(ScrapeJobTarget.status, func.count())
        .where(
            ScrapeJobTarget.workspace_id == workspace_id,
            ScrapeJobTarget.scrape_job_id == scrape_job_id,
        )
        .group_by(ScrapeJobTarget.status)
    )
    by_status: dict[ScrapeTargetStatus, int] = dict(session.execute(stmt).all())

    success = by_status.get(ScrapeTargetStatus.COMPLETED, 0)
    failure = by_status.get(ScrapeTargetStatus.FAILED, 0)
    skipped = by_status.get(ScrapeTargetStatus.SKIPPED, 0)
    total = sum(by_status.values())
    return Counts(success=success, failure=failure, skipped=skipped, total=total)
