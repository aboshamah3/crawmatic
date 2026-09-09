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
  registry entry), now TWO gates that must BOTH pass (EPA C8, F13):

  1. **Per-key coverage** — a ``(workspace_id, product_variant_id,
     date)`` level ``EXCEPT`` check: every key that had a source
     observation in the partition's range must also have >=1
     ``variant_price_daily_rollups`` row for that exact
     ``(workspace, variant, date)``. Date-level coverage alone (the
     pre-C8 gate) could pass while a single tenant/variant's rollup was
     silently missing, because it only asked "did *some* rollup land
     that day", not "did *every* key's rollup land".
  2. **Completion watermark** — every UTC date in the partition's range
     must have a ``rollup_completion`` row (C7's table,
     ``app_shared.maintenance.rollup_sql``) with ``complete = true``.
     A day whose keyset batch loop stopped partway (killed at the
     Celery ``time_limit``, e.g.) can already have *some* correct
     per-key rows for the pairs it reached — gate 1 alone would pass on
     those pairs and never notice the day was cut short. The watermark
     answers "did the rollup run finish the whole day", independent of
     how many keys existed.

  Either gate failing means the partition is retained and reported
  ``partitions_skipped_pending_rollups`` — a later retention pass
  re-checks both (self-healing, never silently dropped, R7). The
  completion table being unmigrated is treated the same as "not
  complete" (fail closed: never drop on the *absence* of evidence).

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
from app_shared.maintenance.registry import (
    PARTITIONED_TABLES,
    RETENTION_FAMILIES,
    RetentionFamily,
    RetentionMechanism,
    retention_class_enabled,
    retention_days,
    retention_family_days,
)
from app_shared.maintenance.rollup_sql import ROLLUP_COMPLETION_TABLE, completion_store_available


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
    """Build the (unexecuted) per-key verify-before-drop ``EXCEPT``
    statement (contracts/retention-drop.md ``rollups_cover``, EPA C8
    F13: gate 1 of 2).

    Split out so its rendered SQL can be asserted in a pure unit test
    without a live DB or session (mirrors
    ``app_shared.maintenance.partitions._to_regclass_stmt``). Every
    distinct ``(workspace_id, product_variant_id, date)`` key that had a
    source observation in the partition, minus every key already covered
    by a ``variant_price_daily_rollups`` row in ``[d0, d_n)`` — non-empty
    means at least one key's date is missing its rollup.

    Per-key, not date-only (EPA C8, F13): the pre-C8 gate compared bare
    dates, so ONE workspace's rollup landing that day was enough to mark
    the whole day "covered" even if every other tenant/variant's rollup
    for that day was missing. Comparing the full
    ``(workspace_id, product_variant_id, date)`` triple closes that hole
    — a date only counts as covered for the specific key it was actually
    computed for.

    ``(scraped_at AT TIME ZONE 'UTC')::date`` and not the bare
    ``scraped_at::date``: a plain cast of a ``timestamptz`` resolves in
    the **session** ``TimeZone``, whereas the rollup rows this is
    compared against are keyed on the **UTC** day
    (``app_shared.maintenance.rollups.utc_day_bounds`` /
    ``default_target_date``). Production runs ``TimeZone = Etc/UTC`` so
    the two agree today; pinning UTC here makes that agreement a
    property of the SQL rather than of a session setting — a mismatch
    would manufacture "uncovered" edge dates and wedge the
    verify-before-drop gate permanently (partitions never dropped).
    This scan is deliberately NOT sargable-rewritten: it must visit every
    row of the partition it is validating, and the partition is already
    named directly (no pruning to gain).
    """
    return text(
        f"""
        SELECT DISTINCT workspace_id, product_variant_id,
               (scraped_at AT TIME ZONE 'UTC')::date AS date
        FROM {partition_name}
        EXCEPT
        SELECT DISTINCT workspace_id, product_variant_id, date
        FROM variant_price_daily_rollups
          WHERE date >= :d0 AND date < :d_n
        """
    ).bindparams(d0=d0, d_n=d_n)


def _completion_watermark_gap_stmt(d0: date_type, d_n: date_type):
    """Build the (unexecuted) statement finding any UTC calendar date in
    ``[d0, d_n)`` whose ``rollup_completion`` watermark is missing or not
    yet ``complete`` (C7's table; EPA C8 F13: gate 2 of 2).

    Every calendar day in the range is generated (not just days that had
    observations) because the daily-rollup batch loop
    (``app_shared.maintenance.rollups.run_daily_rollup``) writes a
    ``complete=true`` checkpoint for a day even when it has ZERO driver
    rows — the loop still runs once, finds nothing, and reports
    ``day_done``. So an absent completion row for a day the scheduler
    should have covered is itself evidence: either the run never
    happened yet or it was killed mid-batch, and either way the
    partition must be retained. ``LEFT JOIN ... WHERE rc.date IS NULL``
    (rather than reading each day one at a time) keeps this one round
    trip regardless of the partition's span.
    """
    # `CAST(:name AS type)`, not `:name::type` -- SQLAlchemy `text()`'s
    # bind-parameter regex has a negative lookahead against a following
    # colon, so `:d0::timestamp` is never recognized as bind parameter
    # `d0` at all (silently parsed as literal text), which makes
    # `.bindparams(d0=...)` raise "doesn't define a bound parameter
    # named 'd0'". `CAST(... AS ...)` sidesteps the ambiguity entirely.
    return text(
        f"""
        SELECT gs::date AS date
        FROM generate_series(CAST(:d0 AS timestamp),
                              CAST(:d_n AS timestamp) - interval '1 day',
                              interval '1 day') AS gs
        LEFT JOIN {ROLLUP_COMPLETION_TABLE} rc
          ON rc.date = gs::date AND rc.complete = TRUE
        WHERE rc.date IS NULL
        LIMIT 1
        """
    ).bindparams(d0=d0, d_n=d_n)


def rollups_cover(session: Session, part: PartitionBounds) -> bool:
    """Verify-before-drop gate (FR-016, US3 AS-2, SC-004; EPA C8 F13) —
    ``True`` iff BOTH:

    1. every ``(workspace, variant, date)`` key in ``part``'s range that
       had source observations also has a covering
       ``variant_price_daily_rollups`` row (:func:`_rollups_cover_stmt`),
       **and**
    2. every UTC calendar date in ``part``'s range has a
       ``rollup_completion`` row with ``complete = true``
       (:func:`_completion_watermark_gap_stmt`) — the day's rollup batch
       loop ran all the way to the end, not just partway.

    Gate 1 alone can pass on a day a batch run was killed mid-loop: the
    pairs it reached before the kill already have correct rows, so a
    per-key comparison sees no gap for *those* pairs and would let the
    partition drop while pairs after the cursor were never rolled up at
    all. Gate 2 closes that: a day is only "done" once its checkpoint
    says so.

    If ``rollup_completion`` itself is not migrated yet, gate 2 fails
    closed (never drop on the absence of evidence) rather than being
    skipped — this only matters immediately after C7 lands and before
    the migration runs; once applied, the table always exists after this
    point onward.

    Gate 1 is key-scoped to what the partition actually contains: a key
    with **no** source data needs no rollup at all. Gate 2 is
    deliberately NOT scoped to days with data — it requires a
    ``complete`` checkpoint for every calendar date in ``[d0, d_n)``,
    because the daily-rollup scheduler is expected to invoke (and
    checkpoint) every UTC day regardless of whether that day turns out
    to have zero observations; an unexpectedly absent watermark for a
    quiet day is itself evidence the scheduler did not run, not proof
    there was nothing to roll up. Both are inherently cross-tenant (one
    partition holds every workspace's rows; the watermark spans all
    workspaces for a day), hence the unscoped reads on the system
    session.
    """
    d0, d_n = part.start.date(), part.end.date()
    missing_keys = session.execute(  # noqa: workspace-scope
        _rollups_cover_stmt(part.name, d0, d_n)
    ).first()
    if missing_keys is not None:
        return False
    if not completion_store_available(session):  # noqa: workspace-scope
        return False
    missing_watermark = session.execute(  # noqa: workspace-scope
        _completion_watermark_gap_stmt(d0, d_n)
    ).first()
    return missing_watermark is None


@dataclass
class RunReport:
    """Structured summary of one ``run_retention`` run (FR-023,
    data-model.md §5) — logged by the Celery task wrapper, never
    persisted."""

    tables_skipped_absent: list[str] = field(default_factory=list)
    partitions_dropped: list[str] = field(default_factory=list)
    partitions_skipped_pending_rollups: list[str] = field(default_factory=list)
    rollup_rows_deleted: int = 0
    #: EPA C9 (F14). Retention classes the OWNER has not enabled in
    #: `Settings.RETENTION_ENABLED_CLASSES`. On a fresh deployment this
    #: holds EVERY registered class and every other list above is empty —
    #: which is the intended, ratification-gated posture, not a fault.
    classes_skipped_not_enabled: list[str] = field(default_factory=list)
    #: EPA C9 (F14). ``{class_key: rows}`` for the `ROW_DELETE` families
    #: (the non-partitioned ones, and the terminal-state subsets).
    rows_deleted_by_class: dict[str, int] = field(default_factory=dict)


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


def _row_delete_stmt(family: RetentionFamily, batch_size: int):
    """Build the (unexecuted) BOUNDED row-retention ``DELETE`` for a
    ``ROW_DELETE`` family (EPA C9, F14).

    Bounded three ways, and each bound is load-bearing:

    * ``LIMIT`` inside a subquery — an unbounded ``DELETE`` over a
      multi-million-row table is one enormous transaction, one enormous
      WAL record, and a lock held for its whole duration. The caller
      loops instead.
    * ``family.row_predicate`` — the terminal-state filter. Without it
      this would age out rows the system is still working on: a
      ``PENDING`` scrape target, a ``POSTED`` dispatch intent whose
      ambiguity is the whole reason the row exists, a ``RESERVED`` cost
      lease that is holding real money against a budget.
    * The GROUP column, not ``ctid`` — when ``family.group_column`` is
      set the batch is chosen by DISTINCT GROUP VALUE, so a group is
      deleted whole or not at all. ``network_operation_allocations``
      needs this: its DEFERRED constraint trigger re-checks at COMMIT
      that one operation's allocations sum exactly to that operation's
      cost, so a batch boundary falling INSIDE an allocation set would
      raise a spurious shortfall and abort the pass.

    ``row_predicate``/``group_column``/``timestamp_column`` are code
    constants from the registry, never user input — rendered directly
    into the statement exactly like the migration-time ``op.execute``
    convention (research R2).
    """
    predicate = f" AND {family.row_predicate}" if family.row_predicate else ""
    if family.group_column:
        return text(
            f"""
            DELETE FROM {family.table}
            WHERE {family.group_column} IN (
                SELECT {family.group_column}
                FROM {family.table}
                WHERE {family.timestamp_column} < :cutoff{predicate}
                GROUP BY {family.group_column}
                HAVING max({family.timestamp_column}) < :cutoff
                LIMIT {int(batch_size)}
            )
            """
        )
    return text(
        f"""
        DELETE FROM {family.table}
        WHERE ctid IN (
            SELECT ctid FROM {family.table}
            WHERE {family.timestamp_column} < :cutoff{predicate}
            LIMIT {int(batch_size)}
        )
        """
    )


def _delete_expired_rows(
    session: Session,
    family: RetentionFamily,
    *,
    cutoff: datetime,
    batch_size: int,
    max_batches: int,
) -> int:
    """Delete ``family``'s expired rows in bounded batches; return the count.

    One transaction per batch (``session.commit()``), so a killed
    invocation keeps the batches it finished and the next pass resumes
    from what is left — the same durable-partial-progress posture C7's
    rollup loop takes. Stops early when a batch deletes nothing (no more
    work) or when ``max_batches`` is reached (there IS more work, and the
    next invocation should do it rather than this one running unboundedly
    past its Celery time limit).
    """
    deleted = 0
    stmt = _row_delete_stmt(family, batch_size)
    for _ in range(max_batches):
        result = session.execute(stmt, {"cutoff": cutoff})  # noqa: workspace-scope
        rows = result.rowcount if result.rowcount is not None else 0
        session.commit()
        deleted += rows
        if rows == 0:
            break
    return deleted


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
        # EPA C9 (F14): the owner-ratification switch, checked BEFORE the
        # table is even probed. `RETENTION_ENABLED_CLASSES` ships empty,
        # so on an un-ratified deployment this loop drops nothing at all
        # and says so. A retention window in `Settings` is a PROPOSAL
        # until the owner signs the matching line in
        # `docs/RETENTION_POLICY.md` and names the class.
        if not retention_class_enabled(entry.name, settings):
            report.classes_skipped_not_enabled.append(entry.name)
            continue

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
    if retention_class_enabled("variant_price_daily_rollups", settings):
        rollup_cutoff_date = now_utc.astimezone(timezone.utc).date() - timedelta(
            days=settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS
        )
        result = session.execute(  # noqa: workspace-scope
            _rollup_age_delete_stmt(rollup_cutoff_date)
        )
        report.rollup_rows_deleted = (
            result.rowcount if result.rowcount is not None else 0
        )
    else:
        report.classes_skipped_not_enabled.append("variant_price_daily_rollups")

    # --- Part C: bounded row retention for the non-partitioned families -----
    # (EPA C9, F14.) `variant_price_daily_rollups` is handled by Part B
    # above and skipped here, because its cutoff is a DATE against a
    # `date` column rather than a timestamp — the one family whose clock
    # is a calendar day.
    for family in RETENTION_FAMILIES:
        if family.mechanism is not RetentionMechanism.ROW_DELETE:
            continue
        if family.table == "variant_price_daily_rollups":
            continue
        if not retention_class_enabled(family.class_key, settings):
            report.classes_skipped_not_enabled.append(family.class_key)
            continue
        if not table_exists(session, family.table):
            report.tables_skipped_absent.append(family.table)
            continue
        report.rows_deleted_by_class[family.class_key] = _delete_expired_rows(
            session,
            family,
            cutoff=now_utc - timedelta(days=retention_family_days(family, settings)),
            batch_size=settings.RETENTION_ROW_DELETE_BATCH_SIZE,
            max_batches=settings.RETENTION_ROW_DELETE_MAX_BATCHES,
        )

    return report
