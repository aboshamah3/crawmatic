"""Maintenance **outcome** assertions (2026-08-15 readiness cycle).

The root problem behind the 2026-08-15 partition/rollup incident was not
that maintenance failed — things fail — it was that it failed *silently
for a month*, and the only reason anyone found out was a manual database
inspection 17 days before a dated total write outage.

Every existing signal in this path reports on the *attempt*: the
scheduler logs that it enqueued a task, the task logs its own run report
when it runs. None of them fire when the task never runs at all, which is
precisely what happened — twice over, for two independent reasons:

1. the scheduler's in-process cadence accumulators reset on restart (see
   :mod:`app_shared.maintenance.cadence`), and
2. the ``worker`` service had neither ``SYSTEM_DATABASE_URL`` nor
   ``AUTH_DATABASE_URL``, so every maintenance task that reached it died
   in ``get_system_session()`` with a ``RuntimeError`` before doing any
   work.

Cause 2 is the reason this module asserts on **outcomes read from the
database** rather than on the scheduler's own bookkeeping. A cadence
counter would have looked perfectly healthy the whole time: the scheduler
*was* enqueueing, the worker *was* receiving, and the partition still did
not exist. The only trustworthy question is "does the partition the
calendar will need in N days exist, right now?".

Two assertions, both ERROR-level structured logs following the
`contracts/observability.md` ``event key=value`` convention:

* :data:`EVENT_PARTITION_MISSING` — a required upcoming monthly partition
  does not exist. This is the one that becomes a write outage on a known
  date, so it is reported with the date it becomes fatal.
* :data:`EVENT_ROLLUP_STALE` — ``variant_price_daily_rollups`` has
  produced no row for N days. This one is quieter but compounds: while it
  holds, retention's ``rollups_cover()`` gate correctly refuses to drop
  any ``price_observations`` partition, so storage grows without bound.

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.maintenance.partitions import (
    month_partition_bounds,
    partition_name,
    table_exists,
)
from app_shared.maintenance.registry import PARTITIONED_TABLES

logger = logging.getLogger("maintenance.health")

#: ERROR — a required upcoming monthly partition is absent. Fields:
#: ``table``, ``partition``, ``month_start``, ``days_until_writes_fail``.
EVENT_PARTITION_MISSING = "maintenance_partition_missing"

#: ERROR — the daily rollup has produced no rows for too long. Fields:
#: ``last_rollup_date``, ``days_stale``, ``threshold_days``.
EVENT_ROLLUP_STALE = "maintenance_rollup_stale"

#: INFO — a clean health pass, so the absence of ERROR lines is
#: distinguishable from the health check itself never running.
EVENT_HEALTH_OK = "maintenance_health_ok"


@dataclass(frozen=True)
class MissingPartition:
    """One required-but-absent monthly partition."""

    table: str
    partition: str
    month_start: datetime

    def days_until_writes_fail(self, now: datetime) -> int:
        """Days until an INSERT would land in this absent partition.

        ``0`` (or negative, clamped to ``0``) means writes into it fail
        *now* — the month has already started.
        """
        return max(0, (self.month_start - now).days)


@dataclass
class HealthReport:
    """Structured result of one :func:`check_maintenance_health` pass.

    Exposed (not just logged) so another workstream can poll it from an
    admin endpoint or a probe without re-implementing the catalog reads.
    """

    missing_partitions: list[MissingPartition] = field(default_factory=list)
    tables_skipped_absent: list[str] = field(default_factory=list)
    last_rollup_date: date | None = None
    rollup_days_stale: int | None = None
    rollup_stale: bool = False

    @property
    def healthy(self) -> bool:
        return not self.missing_partitions and not self.rollup_stale


def find_missing_partitions(
    session: Session, *, now_utc: datetime, months_ahead: int
) -> tuple[list[MissingPartition], list[str]]:
    """Return ``(missing, tables_skipped_absent)`` for months ``0..months_ahead``.

    Offset ``0`` is the current month — a missing current-month partition
    means writes are failing *already*, so it is deliberately included
    rather than treated as "in the past". A registered-but-absent parent
    table (``webhook_events`` before SPEC-16) is skipped via the same
    ``to_regclass`` gate the creation path uses, never reported as
    missing.
    """
    missing: list[MissingPartition] = []
    skipped: list[str] = []
    for entry in PARTITIONED_TABLES:
        if not table_exists(session, entry.name):
            skipped.append(entry.name)
            continue
        for offset in range(months_ahead + 1):
            suffix, start, _end = month_partition_bounds(now_utc, offset)
            child = partition_name(entry.name, suffix)
            if table_exists(session, child):
                continue
            missing.append(
                MissingPartition(table=entry.name, partition=child, month_start=start)
            )
    return missing, skipped


def latest_rollup_date(session: Session) -> date | None:
    """Return the most recent ``variant_price_daily_rollups.date``, or ``None``.

    Deliberately cross-tenant and unscoped — this is an operator health
    probe over a platform-level maintenance outcome, not a tenant read;
    it returns a single date and no row contents.
    """
    if not table_exists(session, "variant_price_daily_rollups"):
        return None
    return session.execute(
        text("SELECT max(date) FROM variant_price_daily_rollups")  # noqa: workspace-scope
    ).scalar()


def check_maintenance_health(
    session: Session,
    *,
    now_utc: datetime | None = None,
    months_ahead: int,
    rollup_stale_after_days: int,
) -> HealthReport:
    """Run both outcome assertions and return the report (no logging).

    ``months_ahead`` should match the partition-creation lookahead so the
    assertion asks for exactly what the creator promises to have made.
    ``rollup_stale_after_days`` tolerates the normal lag: the rollup runs
    daily for *yesterday*, so a healthy system is always at least one day
    behind.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    report = HealthReport()
    report.missing_partitions, report.tables_skipped_absent = find_missing_partitions(
        session, now_utc=now_utc, months_ahead=months_ahead
    )

    report.last_rollup_date = latest_rollup_date(session)
    if report.last_rollup_date is None:
        # Never produced a row at all. Only stale once the system has had
        # a chance to produce one -- but there is no way to distinguish
        # "brand new install" from "broken for a month" from this table
        # alone, so fall back to whether there is anything to roll up.
        report.rollup_days_stale = None
        report.rollup_stale = _has_observations_older_than(
            session, now_utc - timedelta(days=rollup_stale_after_days)
        )
    else:
        report.rollup_days_stale = (now_utc.date() - report.last_rollup_date).days
        report.rollup_stale = report.rollup_days_stale > rollup_stale_after_days

    return report


def _has_observations_older_than(session: Session, cutoff: datetime) -> bool:
    """True if any ``price_observations`` row predates ``cutoff``.

    Distinguishes "the rollup has never run" (rows exist that should have
    been rolled up) from "there is genuinely nothing to roll up yet" (a
    fresh install), so a brand-new deployment does not page anyone.
    Unscoped by design: a platform-level existence probe returning a
    boolean, not tenant data.
    """
    if not table_exists(session, "price_observations"):
        return False
    return bool(
        session.execute(
            text(  # noqa: workspace-scope
                "SELECT EXISTS (SELECT 1 FROM price_observations WHERE scraped_at < :cutoff)"
            ).bindparams(cutoff=cutoff)
        ).scalar()
    )


def log_health_report(report: HealthReport, *, now_utc: datetime, threshold_days: int) -> None:
    """Emit the ERROR-level structured signals for a report (or one INFO OK).

    This is the only place these event names are produced; alerting keys
    off :data:`EVENT_PARTITION_MISSING` / :data:`EVENT_ROLLUP_STALE`.
    """
    for missing in report.missing_partitions:
        logger.error(
            "%s table=%s partition=%s month_start=%s days_until_writes_fail=%d",
            EVENT_PARTITION_MISSING,
            missing.table,
            missing.partition,
            missing.month_start.date().isoformat(),
            missing.days_until_writes_fail(now_utc),
        )

    if report.rollup_stale:
        logger.error(
            "%s last_rollup_date=%s days_stale=%s threshold_days=%d",
            EVENT_ROLLUP_STALE,
            report.last_rollup_date.isoformat() if report.last_rollup_date else "never",
            report.rollup_days_stale if report.rollup_days_stale is not None else "never",
            threshold_days,
        )

    if report.healthy:
        logger.info(
            "%s months_ahead_ok=1 last_rollup_date=%s tables_skipped_absent=%s",
            EVENT_HEALTH_OK,
            report.last_rollup_date.isoformat() if report.last_rollup_date else "never",
            report.tables_skipped_absent,
        )


__all__ = [
    "EVENT_HEALTH_OK",
    "EVENT_PARTITION_MISSING",
    "EVENT_ROLLUP_STALE",
    "HealthReport",
    "MissingPartition",
    "check_maintenance_health",
    "find_missing_partitions",
    "latest_rollup_date",
    "log_health_report",
]
