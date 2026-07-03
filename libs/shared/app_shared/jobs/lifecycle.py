"""Deterministic job finalization + stall-window bucketing.

Per ``contracts/lifecycle-counters.md`` (D6, FR-019, FR-020). Pure — no
DB/Redis/network, no scrapy/twisted/fastapi.

:func:`resolve_finalized_status` is the single ordered, **failure-centric**
rule the ``finalize_jobs`` maintenance task (``apps/workers/app/workers/
tasks_jobs.py``) uses to resolve a job's terminal :class:`~app_shared.
enums.ScrapeJobStatus` from its aggregated target counts
(:func:`app_shared.jobs.targets.aggregate_counts`).

:func:`stall_window` derives the idempotency-window bucket
``recover_stalled_batches`` folds into its re-dispatch ``batch_index``
suffix (``contracts/stall-recovery.md``) so a duplicate recovery delivery
within one window is neutralized by the reused Redis ``SET NX`` guard,
while the next window mints a fresh key permitting a genuine retry.
"""

from __future__ import annotations

from datetime import datetime

from app_shared.enums import ScrapeJobStatus

__all__ = ["resolve_finalized_status", "stall_window"]


def resolve_finalized_status(
    success: int, failure: int, skipped: int, total: int
) -> ScrapeJobStatus:
    """Resolve a job's terminal status from its aggregated target counts.

    Single ordered, **failure-centric** rule — skips are non-fatal
    (analyze A1 remediation):

    1. ``total == 0`` -> **COMPLETED** (a zero-target job, e.g. a
       zero-active-match variant run, FR-020).
    2. ``failure == 0`` -> **COMPLETED** (covers all-success,
       success+skipped, and skipped-only — nothing actually failed).
    3. ``failure > 0`` and ``success > 0`` -> **PARTIAL_FAILED**.
    4. ``failure > 0`` and ``success == 0`` -> **FAILED**.
    """
    del skipped  # non-fatal — folded in only via its absence from `failure` (rule 2).
    if total == 0:
        return ScrapeJobStatus.COMPLETED
    if failure == 0:
        return ScrapeJobStatus.COMPLETED
    if success > 0:
        return ScrapeJobStatus.PARTIAL_FAILED
    return ScrapeJobStatus.FAILED


def stall_window(now: datetime, timeout_seconds: int) -> int:
    """The stall-recovery idempotency bucket: ``floor(now.timestamp() / timeout_seconds)``.

    Two ``recover_stalled_batches`` deliveries whose ``now`` falls inside
    the same window derive the same bucket -> the same suffixed
    ``batch_index`` -> the client's Redis ``SET NX`` guard neutralizes the
    duplicate re-dispatch; the next window mints a fresh bucket, allowing
    a genuine later retry if the batch is still stalled.
    """
    return int(now.timestamp() // timeout_seconds)
