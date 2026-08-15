"""Outbox reconciliation + retention sweep (audit H1).

The drain (:mod:`app_shared.outbox.dispatcher`) is the happy path. This
module is the safety net that makes the outbox operable in production:

* **Stuck detection.** Reports the oldest PENDING message's age and how
  many messages are overdue past ``stuck_after_seconds``. A rising
  ``oldest_pending_age_seconds`` is the single number that says "async
  work is silently piling up" — precisely the signal audit §H5 says is
  missing today.
* **Dead-letter visibility.** Counts ``DEAD`` rows and logs at ERROR when
  there are any. A DEAD row means committed domain work never reached a
  worker after ``OUTBOX_MAX_ATTEMPTS`` tries; it is an alertable state,
  never silently discarded.
* **Retention.** Deletes PUBLISHED rows older than the retention window
  so the table cannot grow unbounded, and (with a much longer window)
  ages out DEAD rows so a permanently broken producer cannot fill the
  disk either.

Why retention lives here rather than in ``app_shared.maintenance``:
``run_retention`` operates on *partitioned* tables registered in
``PARTITIONED_TABLES`` — it reclaims space by dropping whole monthly
partitions and never bulk-DELETEs a raw table (R7; the one sanctioned
exception is the non-partitioned rollup table). ``outbox_messages`` is
deliberately not partitioned (see ``app_shared.models.outbox``), so it
cannot be served by that machinery; its cleanup is a small, bounded
DELETE of *terminal* rows only, run on the same scheduler cadence as the
other maintenance passes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app_shared.enums import OutboxStatus
from app_shared.models.outbox import OutboxMessage

logger = logging.getLogger("app_shared.outbox.reconciler")

__all__ = ["SweepReport", "sweep_outbox"]

#: DEAD rows are kept this many times longer than PUBLISHED ones — a dead
#: letter is evidence for an incident review, not routine exhaust.
DEAD_RETENTION_MULTIPLIER = 10


@dataclass(frozen=True)
class SweepReport:
    """One reconciliation pass's findings."""

    pending: int = 0
    stuck: int = 0
    dead: int = 0
    oldest_pending_age_seconds: float | None = None
    published_deleted: int = 0
    dead_deleted: int = 0


def _count(session: Session, status: OutboxStatus) -> int:
    stmt = select(func.count()).select_from(OutboxMessage).where(  # noqa: workspace-scope
        OutboxMessage.status == status
    )
    return int(session.execute(stmt).scalar_one() or 0)


def sweep_outbox(
    session: Session,
    *,
    now: datetime,
    stuck_after_seconds: int,
    retention_days: int,
) -> SweepReport:
    """Report backlog/dead-letter health and delete aged terminal rows.

    Runs on the BYPASSRLS system session — like the drain, every query
    here is inherently cross-tenant (annotated ``# noqa: workspace-scope``)
    and touches only the outbox's own bookkeeping columns, never a
    workspace's domain rows. The caller commits.
    """
    pending = _count(session, OutboxStatus.PENDING)
    dead = _count(session, OutboxStatus.DEAD)

    stuck_cutoff = now - timedelta(seconds=stuck_after_seconds)
    stuck_stmt = (
        select(func.count())  # noqa: workspace-scope
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.available_at <= stuck_cutoff,
        )
    )
    stuck = int(session.execute(stuck_stmt).scalar_one() or 0)

    oldest_stmt = (
        select(func.min(OutboxMessage.created_at))  # noqa: workspace-scope
        .select_from(OutboxMessage)
        .where(OutboxMessage.status == OutboxStatus.PENDING)
    )
    oldest_created_at = session.execute(oldest_stmt).scalar_one_or_none()
    oldest_age = (
        (now - oldest_created_at).total_seconds() if oldest_created_at is not None else None
    )

    published_cutoff = now - timedelta(days=retention_days)
    published_deleted = int(
        session.execute(
            delete(OutboxMessage).where(  # noqa: workspace-scope
                OutboxMessage.status == OutboxStatus.PUBLISHED,
                OutboxMessage.updated_at < published_cutoff,
            )
        ).rowcount
        or 0
    )

    dead_cutoff = now - timedelta(days=retention_days * DEAD_RETENTION_MULTIPLIER)
    dead_deleted = int(
        session.execute(
            delete(OutboxMessage).where(  # noqa: workspace-scope
                OutboxMessage.status == OutboxStatus.DEAD,
                OutboxMessage.updated_at < dead_cutoff,
            )
        ).rowcount
        or 0
    )

    report = SweepReport(
        pending=pending,
        stuck=stuck,
        dead=dead,
        oldest_pending_age_seconds=oldest_age,
        published_deleted=published_deleted,
        dead_deleted=dead_deleted,
    )

    log = logger.error if (dead or stuck) else logger.info
    log(
        "outbox_reconcile pending=%d stuck=%d dead=%d oldest_pending_age_seconds=%s "
        "published_deleted=%d dead_deleted=%d",
        report.pending,
        report.stuck,
        report.dead,
        report.oldest_pending_age_seconds,
        report.published_deleted,
        report.dead_deleted,
    )
    return report
