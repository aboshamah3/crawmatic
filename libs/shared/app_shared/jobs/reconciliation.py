"""Auditable repair for targets failed before a later successful observation.

The 2026-08-24 lifecycle incident left a small class of historical targets in
``FAILED`` even though the same ``(workspace, job, match)`` later produced a
successful observation.  This module is the reusable, workspace-scoped repair
seam.  It defaults to dry-run and requires the exact reviewed match-id set for
an applying run, preventing a stale preview from silently repairing a changed
candidate set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeJobStatus, ScrapeTargetStatus
from app_shared.jobs.lifecycle import resolve_finalized_status
from app_shared.jobs.targets import Counts, aggregate_counts
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.observations import PriceObservation

__all__ = ["TargetReconciliationReport", "reconcile_successful_failed_targets"]


@dataclass(frozen=True)
class TargetReconciliationReport:
    """Serializable preview/result returned by the service and admin task."""

    workspace_id: uuid.UUID
    scrape_job_id: uuid.UUID
    dry_run: bool
    candidate_match_ids: tuple[uuid.UUID, ...]
    before: Counts
    projected: Counts
    job_status_before: ScrapeJobStatus
    job_status_projected: ScrapeJobStatus | None
    applied_count: int

    def as_dict(self) -> dict[str, object]:
        def counts_dict(counts: Counts) -> dict[str, int]:
            return {
                "success": counts.success,
                "failure": counts.failure,
                "skipped": counts.skipped,
                "total": counts.total,
            }

        return {
            "workspace_id": str(self.workspace_id),
            "scrape_job_id": str(self.scrape_job_id),
            "dry_run": self.dry_run,
            "candidate_match_ids": [str(value) for value in self.candidate_match_ids],
            "candidate_count": len(self.candidate_match_ids),
            "before": counts_dict(self.before),
            "projected": counts_dict(self.projected),
            "job_status_before": self.job_status_before.value,
            "job_status_projected": (
                None if self.job_status_projected is None else self.job_status_projected.value
            ),
            "applied_count": self.applied_count,
        }


def _as_uuid_set(values: Iterable[uuid.UUID | str]) -> set[uuid.UUID]:
    return {uuid.UUID(str(value)) for value in values}


def reconcile_successful_failed_targets(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    dry_run: bool = True,
    expected_match_ids: Iterable[uuid.UUID | str] | None = None,
) -> TargetReconciliationReport:
    """Preview or repair false-failed targets for one explicit job.

    A candidate must currently be ``FAILED`` and have at least one successful
    ``price_observations`` row in the same workspace, job, and match.  Applying
    is compare-before-write: callers must pass the exact match IDs returned by
    a reviewed dry-run, and the operation aborts if that set has changed.

    The caller owns commit/rollback.  No write occurs in dry-run mode.
    """

    workspace_uuid = uuid.UUID(str(workspace_id))
    job_uuid = uuid.UUID(str(scrape_job_id))

    job_stmt = select(ScrapeJob).where(
        ScrapeJob.workspace_id == workspace_uuid,
        ScrapeJob.id == job_uuid,
    )
    if not dry_run:
        job_stmt = job_stmt.with_for_update()
    job = session.execute(job_stmt).scalar_one_or_none()
    if job is None:
        raise LookupError(f"scrape job {job_uuid} not found in workspace {workspace_uuid}")
    job_status_before = job.status

    successful_observation = exists(
        select(PriceObservation.id).where(
            PriceObservation.workspace_id == ScrapeJobTarget.workspace_id,
            PriceObservation.scrape_job_id == ScrapeJobTarget.scrape_job_id,
            PriceObservation.match_id == ScrapeJobTarget.match_id,
            PriceObservation.success.is_(True),
        )
    )
    candidate_stmt = (
        select(ScrapeJobTarget)
        .where(
            ScrapeJobTarget.workspace_id == workspace_uuid,
            ScrapeJobTarget.scrape_job_id == job_uuid,
            # FAILED only — and, since EPA A2, pointedly NOT `CANCELLED`.
            # This repair exists to correct a target the scraper wrongly
            # recorded as failed when a successful observation proves
            # otherwise. A cancelled target has no such contradiction to
            # resolve: it was closed by a human decision, and a late
            # observation cannot even exist for it (the persistence-side
            # cancellation fence refuses to write one). Widening this
            # filter to include CANCELLED would let a straggler silently
            # re-open a job someone deliberately closed.
            ScrapeJobTarget.status == ScrapeTargetStatus.FAILED,
            successful_observation,
        )
        .order_by(ScrapeJobTarget.match_id)
    )
    if not dry_run:
        candidate_stmt = candidate_stmt.with_for_update()
    candidates = list(session.execute(candidate_stmt).scalars().all())
    candidate_ids = tuple(target.match_id for target in candidates)

    before = aggregate_counts(session, job_uuid, workspace_uuid)
    projected = Counts(
        success=before.success + len(candidates),
        failure=before.failure - len(candidates),
        skipped=before.skipped,
        total=before.total,
    )
    terminal_count = projected.success + projected.failure + projected.skipped
    # Cancellation is an explicit operator decision, not a derived roll-up.
    # Repair the target classifications and counters without rewriting that
    # audit state as COMPLETED/PARTIAL_FAILED merely because every target is
    # now terminal.
    projected_status = (
        ScrapeJobStatus.CANCELLED
        if job_status_before == ScrapeJobStatus.CANCELLED
        else (
            resolve_finalized_status(
                projected.success,
                projected.failure,
                projected.skipped,
                projected.total,
            )
            if terminal_count == projected.total
            else None
        )
    )

    if dry_run:
        return TargetReconciliationReport(
            workspace_id=workspace_uuid,
            scrape_job_id=job_uuid,
            dry_run=True,
            candidate_match_ids=candidate_ids,
            before=before,
            projected=projected,
            job_status_before=job_status_before,
            job_status_projected=projected_status,
            applied_count=0,
        )

    if expected_match_ids is None:
        raise ValueError("expected_match_ids from a reviewed dry-run are required when applying")
    expected_ids = _as_uuid_set(expected_match_ids)
    actual_ids = set(candidate_ids)
    if expected_ids != actual_ids:
        raise ValueError(
            "reconciliation candidate set changed since dry-run "
            f"(expected={len(expected_ids)}, actual={len(actual_ids)})"
        )

    for target in candidates:
        target.status = ScrapeTargetStatus.COMPLETED
        target.error_code = None
        # Preserve completed_at: this repairs classification, not history.

    # Keep every counter sourced from target rows/the reviewed projection.
    # ``total_targets`` is included so this one-time repair also corrects a
    # stale header left by any earlier lifecycle incident.
    job.total_targets = projected.total
    job.success_count = projected.success
    job.failure_count = projected.failure
    job.skipped_count = projected.skipped
    if projected_status is not None:
        job.status = projected_status
        if job.completed_at is None:
            job.completed_at = datetime.now(timezone.utc)

    return TargetReconciliationReport(
        workspace_id=workspace_uuid,
        scrape_job_id=job_uuid,
        dry_run=False,
        candidate_match_ids=candidate_ids,
        before=before,
        projected=projected,
        job_status_before=job_status_before,
        job_status_projected=projected_status,
        applied_count=len(candidates),
    )
