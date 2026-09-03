"""Maintenance sweeps that un-wedge jobs abandoned mid-flight (EPA A3/B2, 2026-09-03).

**The failure.** A scrapyd container is replaced mid-job — a redeploy, an
OOM kill, a node drained. Every target it had already claimed stays
``STARTED``. The process that would have written the terminal status no
longer exists, so nothing ever writes it: ``finalize_jobs`` never sees
"all targets terminal", the job dangles ``RUNNING`` forever, and the
customer's refresh silently never completes. Neither existing sweep
covers it — ``recover_stalled_batches`` owns only targets still bare
``PENDING``, and ``redispatch_pending_jobs`` only jobs holding
``PENDING``/``DEFERRED`` work.

**Why these are bulk statements and not ``mark_target``.**
``app_shared.jobs.targets.mark_target`` is the transition helper for a
row a *live spider owns*: it exists to serialize a claimant's own
lifecycle writes and to keep per-target invariants with the process that
is doing the work. These two functions are the opposite situation by
construction. Both operate only on rows that provably have no live owner:
:func:`revert_stale_started_targets` waits out
``MATCH_LOCK_BROWSER_TTL_SECONDS`` plus a grace, so the claimant's own
in-flight lock has already expired, and
:func:`fail_targets_past_job_deadline` acts on jobs past a ceiling many
times any legitimate run. Loading N rows to call a per-row helper on each
would buy no safety that the ``WHERE`` clause does not already provide,
and would turn one statement into an unbounded read of every abandoned
target in the fleet on every 60-second tick.

Both run on the BYPASSRLS system session (the sweep is fleet-wide by
nature — a wedged job in any workspace is the thing being fixed), take
``now`` as an argument rather than reading the clock (so the caller's
single ``now`` is shared by both passes and the tests are deterministic),
and return the number of rows they changed so the task can log it. Both
are idempotent: a second run over rows the first already moved matches
nothing.

Neither statement writes ``updated_at`` — ``scrape_job_targets`` carries
``created_at`` only (SPEC-08 §22); the table has no such column.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeErrorCode, ScrapeJobStatus, ScrapeTargetStatus
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

__all__ = ["fail_targets_past_job_deadline", "revert_stale_started_targets"]

#: Non-terminal target statuses the deadline sweep must close out. The
#: terminal set (COMPLETED/FAILED/SKIPPED/CANCELLED) is real history and
#: is never overwritten — a job's counters are derived from it.
_NON_TERMINAL_TARGET_STATUSES = (
    ScrapeTargetStatus.PENDING,
    ScrapeTargetStatus.STARTED,
    ScrapeTargetStatus.DEFERRED,
)


def revert_stale_started_targets(
    session: Session, *, now: datetime, older_than_seconds: int
) -> int:
    """Return STARTED targets orphaned by a vanished spider to PENDING.

    Selects targets that are ``STARTED`` with a ``started_at`` older than
    ``older_than_seconds`` — past that horizon the claimant's own
    in-flight lock (``MATCH_LOCK_BROWSER_TTL_SECONDS``) has expired, so
    the row is provably unowned and no live process can be racing this
    write.

    Clearing ``dispatched_at`` alongside the status is load-bearing, not
    tidiness: both ``redispatch_pending_jobs`` (deciding whether a job
    needs re-enqueueing at all) and ``dispatch_job`` (selecting the rows
    to POST) match ``PENDING AND dispatched_at IS NULL``. A revert that
    left the stamp behind would produce a PENDING target no dispatcher
    ever selects — the original wedge in a new costume. ``locked_at`` is
    cleared for the mirror-image reason: ``recover_stalled_batches``
    treats a non-NULL lock as "live" and skips the row, and
    ``opsmetrics`` reports it as an in-flight target.
    ``dispatch_intent_id`` goes with ``dispatched_at``; the two are
    stamped together by ``stamp_targets_dispatched`` and a dangling
    intent id would outlive the dispatch it identifies.

    ``started_at IS NOT NULL`` is stated explicitly rather than left to
    three-valued logic: ``NULL < cutoff`` is unknown, so a NULL row would
    be excluded anyway, but by accident rather than by intent.

    :returns: the number of targets reverted.
    """
    cutoff = now - timedelta(seconds=older_than_seconds)
    result = session.execute(
        update(ScrapeJobTarget)  # noqa: workspace-scope
        .where(
            ScrapeJobTarget.status == ScrapeTargetStatus.STARTED,
            ScrapeJobTarget.started_at.is_not(None),
            ScrapeJobTarget.started_at < cutoff,
        )
        .values(
            status=ScrapeTargetStatus.PENDING,
            started_at=None,
            dispatched_at=None,
            dispatch_intent_id=None,
            locked_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def fail_targets_past_job_deadline(
    session: Session, *, now: datetime, max_runtime_seconds: int
) -> int:
    """Fail every non-terminal target of a job past its hard runtime ceiling.

    The backstop for what :func:`revert_stale_started_targets` cannot fix
    — a target that keeps being re-dispatched and re-lost, or one whose
    domain has become permanently unreachable. Once a job has been
    ``RUNNING`` for ``max_runtime_seconds``, its remaining work is failed
    ``JOB_DEADLINE_EXCEEDED`` and stamped ``completed_at``, which makes
    every target terminal and lets the very next ``finalize_jobs`` sweep
    resolve and close the job.

    Scoped to ``RUNNING`` jobs with ``started_at`` set, for two distinct
    reasons. A job that never started has no clock to have expired and is
    ``redispatch_pending_jobs``' business; a job already in a terminal
    status is finished history, and sweeping it would rewrite archived
    rows on every tick forever.

    The job filter is expressed as an ``IN (subquery)`` so the whole
    sweep stays one statement — the alternative (select expired job ids,
    then update) is two round-trips and a torn read between them, during
    which a job could finalize normally and have its just-written
    terminal targets overwritten.

    :returns: the number of targets failed.
    """
    job_cutoff = now - timedelta(seconds=max_runtime_seconds)
    expired_jobs = (
        select(ScrapeJob.id)  # noqa: workspace-scope
        .where(
            ScrapeJob.status == ScrapeJobStatus.RUNNING,
            ScrapeJob.started_at.is_not(None),
            ScrapeJob.started_at < job_cutoff,
        )
        .scalar_subquery()
    )
    result = session.execute(
        update(ScrapeJobTarget)  # noqa: workspace-scope
        .where(
            ScrapeJobTarget.scrape_job_id.in_(expired_jobs),
            ScrapeJobTarget.status.in_(_NON_TERMINAL_TARGET_STATUSES),
        )
        .values(
            status=ScrapeTargetStatus.FAILED,
            error_code=ScrapeErrorCode.JOB_DEADLINE_EXCEEDED,
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
