"""Retention / partition-drop job (SPEC-15 US3, contracts/retention-drop.md,
research R7).

Drops whole expired monthly partitions of every registered table via
``DROP TABLE`` — **never** a bulk ``DELETE`` on a raw append-heavy table
(FR-015, SC-003). Two building blocks gate a drop:

* :func:`partition_eligible` — deterministic whole-range cutoff
  (FR-018): a partition is eligible only when its **entire** half-open
  ``[start, end)`` range is strictly older than the table's retention
  cutoff (``end <= cutoff``); a partition whose newest possible row
  could still be in-window is left in place.
* :func:`rollups_cover` — the verify-before-drop gate (FR-016), applied
  **only** to ``price_observations`` (the one ``feeds_rollups=True``
  registry entry): a date-level ``EXCEPT`` check confirming every UTC
  date in the partition's range that had source observations also has
  >=1 ``variant_price_daily_rollups`` row. Non-empty (missing dates)
  means the partition is retained and reported
  ``partitions_skipped_pending_rollups`` — a later retention pass
  re-checks (self-healing, never silently dropped, R7).

:func:`run_retention` orchestrates both: **Part A** walks
:data:`~app_shared.maintenance.registry.PARTITIONED_TABLES`, dropping
each entry's eligible (and, if applicable, rollup-verified) partitions;
**Part B** is the ONE sanctioned bulk ``DELETE`` in this feature — an
age-based row cutoff on the small, non-partitioned
``variant_price_daily_rollups`` table (2-year default, R7 /
Complexity Tracking deviation #2) — this table has no partition to
drop, so a bounded ``DELETE`` is its only retention mechanism and is
NOT a raw append-heavy partition (SC-003 targets those).

Both the driver-table existence gate and the coverage check are
inherently cross-tenant (one partition/table spans every workspace), so
this module runs on the BYPASSRLS system session (research R9,
``# noqa: workspace-scope`` on the unscoped catalog/coverage reads); no
workspace-owned row is read or written here at all (only DDL + catalog
probes + the rollup-table age delete, which is not workspace-scoped
because it ages ALL workspaces' rollups past the same cutoff — by
design, R7).

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.config import Settings, get_settings
from app_shared.maintenance.partitions import (
    PartitionBounds,
    drop_partition,
    existing_partitions,
    table_exists,
)
from app_shared.maintenance.registry import PARTITIONED_TABLES, retention_days


def partition_eligible(part: PartitionBounds, cutoff: datetime) -> bool:
    """Return ``True`` iff ``part``'s ENTIRE half-open range is strictly
    older than ``cutoff`` (FR-018, deterministic).

    Eligible only when ``part.end <= cutoff`` — a partition whose newest
    possible row (anything up to, but excluding, ``part.end``) could
    still fall inside the retention window is never eligible, even if
    most of its range already precedes ``cutoff``. This is the
    partition-granular, boundary-deterministic rule contracts/
    retention-drop.md specifies (US3 AS-3, "retention window boundary"
    edge case).
    """
    return part.end <= cutoff


def _rollups_cover_stmt(partition_name: str, d0: date_type, d_n: date_type):
    """Build the (unexecuted) date-level verify-before-drop ``EXCEPT``
    statement (contracts/retention-drop.md ``rollups_cover``).

    Split out so its rendered SQL can be asserted in a pure unit test
    without a live DB or session (mirrors
    ``app_shared.maintenance.partitions._to_regclass_stmt``). Every
    distinct observation date in the partition minus every date already
    covered by a rollup row in ``[d0, d_n)`` — non-empty means at least
    one date is missing its rollup.
    """
    return text(
        f"""
        SELECT DISTINCT scraped_at::date FROM {partition_name}
        EXCEPT
        SELECT DISTINCT date FROM variant_price_daily_rollups
          WHERE date >= :d0 AND date < :d_n
        """
    ).bindparams(d0=d0, d_n=d_n)


def rollups_cover(session: Session, part: PartitionBounds) -> bool:
    """Verify-before-drop gate (FR-016, US3 AS-2, SC-004) — ``True`` iff
    every UTC date in ``part``'s range that had source observations also
    has >=1 covering ``variant_price_daily_rollups`` row.

    Date-level (not per-workspace-per-variant) coverage, per the
    clarified rule (research R7): a date with **no** source data needs
    no rollup at all. Inherently cross-tenant (one partition holds every
    workspace's rows), hence the unscoped read on the system session.
    """
    missing = session.execute(  # noqa: workspace-scope
        _rollups_cover_stmt(part.name, part.start.date(), part.end.date())
    ).first()
    return missing is None


@dataclass
class RunReport:
    """Structured summary of one ``run_retention`` run (FR-023,
    data-model.md §5) — logged by the Celery task wrapper, never
    persisted."""

    tables_skipped_absent: list[str] = field(default_factory=list)
    partitions_dropped: list[str] = field(default_factory=list)
    partitions_skipped_pending_rollups: list[str] = field(default_factory=list)
    rollup_rows_deleted: int = 0


def _rollup_age_delete_stmt(cutoff_date: date_type):
    """Build the (unexecuted) ONE sanctioned bulk ``DELETE`` statement —
    the age-based row retention for the small, non-partitioned
    ``variant_price_daily_rollups`` table (R7, Complexity Tracking
    deviation #2). NOT applied to any raw append-heavy partition
    (SC-003's "0% bulk DELETE" targets those, not this table).
    """
    return text("DELETE FROM variant_price_daily_rollups WHERE date < :cutoff_date").bindparams(
        cutoff_date=cutoff_date
    )


def run_retention(
    session: Session, *, now_utc: datetime, settings: Settings | None = None
) -> RunReport:
    """Run one retention pass (contracts/retention-drop.md).

    **Part A** — for each :data:`~app_shared.maintenance.registry.PARTITIONED_TABLES`
    entry: the ``to_regclass`` existence gate (:func:`~app_shared.maintenance.
    partitions.table_exists`) skips a registered-but-absent table (e.g.
    ``webhook_events``, FR-002) cleanly; otherwise every
    :func:`~app_shared.maintenance.partitions.existing_partitions` child
    whose whole range is past its table's retention cutoff
    (:func:`partition_eligible`, FR-017/018) is dropped via
    :func:`~app_shared.maintenance.partitions.drop_partition` (``DROP
    TABLE IF EXISTS``, FR-015/020) — except for ``feeds_rollups=True``
    entries (only ``price_observations``), which additionally require
    :func:`rollups_cover` (FR-016); a partition failing that check is
    retained and recorded ``partitions_skipped_pending_rollups`` rather
    than dropped. Non-rollup tables drop by age alone (FR-019).

    **Part B** — the one sanctioned bulk ``DELETE`` ages
    ``variant_price_daily_rollups`` rows older than
    ``Settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS`` (default 730
    days) — the rollup table's own retention (R7); it is not partitioned
    so it has no partition to drop.

    Idempotent + concurrency-safe throughout: ``IF EXISTS`` on every
    drop, a re-run over an already-retention-clean state is a no-op
    (FR-020, edge case "double run").
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")
    if settings is None:
        settings = get_settings()

    report = RunReport()

    # --- Part A: partition-drop retention -----------------------------------
    for entry in PARTITIONED_TABLES:
        if not table_exists(session, entry.name):
            report.tables_skipped_absent.append(entry.name)
            continue

        cutoff = now_utc - timedelta(days=retention_days(entry, settings))
        for part in existing_partitions(session, entry.name):  # noqa: workspace-scope
            if not partition_eligible(part, cutoff):
                continue
            if entry.feeds_rollups and not rollups_cover(session, part):
                report.partitions_skipped_pending_rollups.append(part.name)
                continue
            drop_partition(session, part.name)
            report.partitions_dropped.append(part.name)

    # --- Part B: the ONE sanctioned bulk DELETE (non-partitioned rollups) ---
    rollup_cutoff_date = now_utc.astimezone(timezone.utc).date() - timedelta(
        days=settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS
    )
    result = session.execute(_rollup_age_delete_stmt(rollup_cutoff_date))  # noqa: workspace-scope
    report.rollup_rows_deleted = result.rowcount if result.rowcount is not None else 0

    return report
