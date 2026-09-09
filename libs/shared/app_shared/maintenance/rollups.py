"""Daily rollup aggregation job (SPEC-15 US2, contracts/daily-rollup.md, research R6).

For UTC date ``D`` (default: yesterday UTC, the most-recently-COMPLETED
day — clarification), upserts one ``variant_price_daily_rollups`` row per
``(workspace_id, product_variant_id)`` that had >=1 ``price_observations``
row on ``D``:

* **Competitor min/avg/max + comparable count** are computed over the
  **latest eligible observation per ``(workspace, variant, match, day)``**
  (EPA C7, plan F12) — a competitor observed ten times that day counts
  once, so the average is an average over competitors rather than over
  scrape events. Eligible means ``success AND comparable AND price IS
  NOT NULL AND currency == <client currency>`` (FR-011) — a currency
  mismatch (or a failed/unpriced/non-comparable observation) is excluded
  from BOTH the aggregate AND the count. ``comparable`` is the
  *persisted* SPEC-09 decision (already false on a currency mismatch) —
  read, not recomputed (research R6). The aggregation runs **in
  Postgres** (:mod:`app_shared.maintenance.rollup_sql`);
  :func:`aggregate_competitor_prices` is the pure Python statement of
  the same rule, kept as the executable specification the tests
  hand-compute against.
* **``client_price``/``currency``/``latest_alert_type``/``product_id``**
  are read (not recomputed) from the SPEC-09 current-comparison surface
  ``variant_price_states``, scoped by ``workspace_id`` + ``product_variant_id``
  (R6) — the only source of client-price state (no per-day history exists).
* The upsert is keyed on ``(workspace_id, product_variant_id, date)``
  (``ON CONFLICT ... DO UPDATE``, FR-010) — idempotent re-run/backfill.

The driver scan (step 1: "which (workspace, variant) pairs had activity
on D") is inherently cross-tenant — one day spans every workspace — so
it runs unscoped on the BYPASSRLS system session (`# noqa:
workspace-scope`, research R9). Since EPA C7 the driver, the
aggregation and the upsert are **one set-based statement per keyset
batch** of at most ``ROLLUP_BATCH_LIMIT`` pairs
(:mod:`app_shared.maintenance.rollup_sql`) rather than ``1 + 3N``
statements for N pairs; every join inside it is on ``workspace_id`` +
``product_variant_id``, so a row from one workspace can never be joined
to another's state (FR-014). Each batch commits and records a durable
``rollup_completion`` checkpoint, so an invocation killed at the Celery
``time_limit`` resumes from where it stopped instead of restarting the
day.

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, NamedTuple

from sqlalchemy.orm import Session

from app_shared.maintenance.rollup_sql import (
    MIN_UUID,
    ROLLUP_BATCH_LIMIT,
    EVENT_COMPLETION_STORE_ABSENT,
    clear_completion,
    completion_store_available,
    read_completion,
    rollup_batch_stmt,
    upsert_completion,
)
from app_shared.maintenance.rollup_watermark import (
    EVENT_WATERMARK_STORE_ABSENT,
    WATERMARK_DAILY_ROLLUP,
    advance_watermark,
    read_watermark,
    seed_watermark,
    watermark_store_available,
)

# `variant_price_daily_rollups.average_competitor_price` is `NUMERIC(18,4)`
# (`Money`, FR-012) — an arithmetic mean of N observed prices is not
# generally exact at 4 decimal places (e.g. three prices averaged), and
# `Money` REJECTS an over-scale value rather than silently rounding it
# (`app_shared.money.parse_money`) — the right policy for a *comparison
# decision* boundary (`app_shared.alerts.engine`), where silently rounding
# could shift which side of a threshold a value falls on. This is a
# *stored snapshot* column, not a decision boundary, so the average is
# deliberately quantized to the column's own scale here (never silently
# rounding a value that matters to a decision — there is no decision
# here, only a fit-in-the-column requirement).
_MONEY_QUANT = Decimal("0.0001")

logger = logging.getLogger(__name__)


def _utc_today(now_utc: datetime) -> date_type:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")
    return now_utc.astimezone(timezone.utc).date()


def default_target_date(now_utc: datetime) -> date_type:
    """Return the most-recently-COMPLETED UTC calendar day relative to ``now_utc``.

    The daily-rollup job's default cadence target (clarification,
    contracts/daily-rollup.md `Signature`) — "yesterday UTC" — so a day
    is only rolled up once it can no longer receive new observations
    from the current tick.
    """
    return _utc_today(now_utc) - timedelta(days=1)


class ObservationRow(NamedTuple):
    """One raw ``price_observations`` row's fields relevant to competitor
    aggregation — the minimal shape :func:`aggregate_competitor_prices`
    needs, independent of how the rows were fetched (a live query result
    or a fake row in a unit test).

    ``match_id``/``scraped_at`` (EPA C7) are what make the latest-eligible-
    per-match collapse expressible: rows sharing a ``match_id`` are the
    same competitor listing observed repeatedly, and ``scraped_at`` picks
    which of those readings survives. Both default to ``None`` so a
    caller that genuinely has one row per competitor (or is testing the
    aggregate arithmetic alone) need not invent them — see
    :func:`aggregate_competitor_prices` for exactly what ``None`` means
    there.
    """

    price: Decimal | None
    currency: str | None
    success: bool
    comparable: bool
    match_id: uuid.UUID | None = None
    scraped_at: datetime | None = None


@dataclass(frozen=True)
class CompetitorAggregate:
    """The four values written to ``variant_price_daily_rollups`` per
    (workspace, variant, day) for the competitor side (FR-011/012/013)."""

    cheapest: Decimal | None
    average: Decimal | None
    highest: Decimal | None
    comparable_count: int


def aggregate_competitor_prices(
    rows: Iterable[ObservationRow], *, client_currency: str
) -> CompetitorAggregate:
    """Pure aggregation over one (workspace, variant, day)'s raw observations.

    **Latest eligible observation per match, then aggregate** (EPA C7,
    plan F12). Two steps, in this order:

    1. *Eligibility.* Keep only ``success AND comparable AND price IS NOT
       NULL AND currency == client_currency`` (FR-011) — a currency
       mismatch (already flagged ``comparable=False`` by SPEC-09) or any
       failed/unpriced/non-comparable row is excluded from BOTH the
       min/avg/max aggregate AND the count, never merely from the
       aggregate.
    2. *Collapse.* Of the surviving rows, keep exactly ONE per
       ``match_id`` — the one with the greatest ``scraped_at``. A
       competitor whose page was scraped ten times that day therefore
       contributes one price, not ten, so ``comparable_count`` counts
       *competitors* and the average is not weighted by how often each
       competitor happened to be refreshed. Filtering before collapsing
       is deliberate: a competitor whose last read of the day failed
       contributes its most recent *usable* reading instead of vanishing
       from the day.

    Rows with ``match_id is None`` are not collapsed with anything —
    each is its own group. That is the honest reading of "no match
    identity": two rows that cannot be shown to be the same competitor
    must not be assumed to be one. Ties on ``scraped_at`` within a match
    keep the first row seen (Postgres's ``DISTINCT ON`` is likewise
    arbitrary among ties; the values being tied is what makes that safe).

    Zero surviving rows -> NULL ``cheapest``/``average``/``highest`` and
    ``comparable_count=0`` (FR-013) — a valid result, never an error. All
    arithmetic is exact ``Decimal`` (never float); the average is
    quantized to the column's ``NUMERIC(18,4)`` scale with
    ``ROUND_HALF_UP`` (ties away from zero) since an arithmetic mean is
    not generally exact at 4 decimal places — matching the ``ROUND(AVG(
    price), 4)`` the set-based statement issues.

    This is the executable specification of what
    :mod:`app_shared.maintenance.rollup_sql` does server-side; the live
    path does NOT call it (that was the N+1). Tests hand-compute against
    it.
    """
    eligible = [
        row
        for row in rows
        if row.success and row.comparable and row.price is not None and row.currency == client_currency
    ]

    # Collapse to the latest eligible row per match. `None` match ids are
    # never merged -- each keeps its own slot via a unique sentinel key.
    latest: dict[object, ObservationRow] = {}
    for index, row in enumerate(eligible):
        key: object = row.match_id if row.match_id is not None else ("__unmatched__", index)
        incumbent = latest.get(key)
        if incumbent is None:
            latest[key] = row
            continue
        # An absent `scraped_at` cannot claim to be later than a present
        # one; between two absent ones the first seen wins (a tie).
        if row.scraped_at is not None and (
            incumbent.scraped_at is None or row.scraped_at > incumbent.scraped_at
        ):
            latest[key] = row

    prices = [row.price for row in latest.values() if row.price is not None]
    if not prices:
        return CompetitorAggregate(cheapest=None, average=None, highest=None, comparable_count=0)

    total = sum(prices, Decimal(0))
    average = (total / Decimal(len(prices))).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    return CompetitorAggregate(
        cheapest=min(prices),
        average=average,
        highest=max(prices),
        comparable_count=len(prices),
    )


def utc_day_bounds(target_date: date_type) -> tuple[datetime, datetime]:
    """Return the half-open UTC instant range ``[start, end)`` covering
    ``target_date`` — i.e. ``[D 00:00:00+00, D+1 00:00:00+00)``.

    Every ``price_observations`` predicate in this module is expressed
    against these two **tz-aware UTC** bounds rather than as
    ``scraped_at::date = D`` (SARGABILITY, and timezone determinism):

    * *Sargability* — wrapping the partition key ``scraped_at`` in a
      cast makes the predicate opaque to the planner: it defeats BOTH
      partition pruning (every monthly partition is scanned, including
      months that cannot possibly contain ``D``) and the
      ``(workspace_id, scraped_at)`` index, forcing a seq scan of the
      whole table per (workspace, variant) probe — quadratic in
      (variants x observations). A plain range comparison against the
      partition key prunes to the one partition and drives the index.
    * *Timezone determinism* — ``timestamptz::date`` resolves in the
      **session** ``TimeZone``, so what "day D" means would silently
      follow a server/role/connection setting. Production runs
      ``TimeZone = Etc/UTC`` and the partitions are cut on UTC month
      boundaries, so the previous cast happened to mean "UTC day", which
      is what this job documents and what
      :func:`default_target_date`/``variant_price_daily_rollups.date``
      mean. These bounds pin that UTC meaning explicitly in the
      *parameters* rather than inheriting it from session state, so the
      behaviour is unchanged today and can no longer be shifted by
      hours by a ``SET TimeZone``.

    The range is half-open: ``D 00:00:00.000000+00`` is included,
    ``D+1 00:00:00+00`` is not — exactly the set of instants
    ``scraped_at::date = D`` selected under UTC.
    """
    start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


@dataclass
class RunReport:
    """Structured summary of one ``run_daily_rollup`` run (FR-023,
    data-model.md §5) — logged by the Celery task wrapper, never
    persisted.

    ``complete`` is the field that decides whether the caller must
    re-enqueue: ``False`` means the invocation stopped on its batch or
    time budget with pairs still unprocessed, and the durable
    ``rollup_completion`` checkpoint holds where to resume. It is also
    the value written to that table's ``complete`` column, which C8's
    retention gate reads before allowing a day's source partition to be
    dropped.
    """

    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)
    #: Statements issued: one per keyset batch (was ``1 + 3N``).
    batches: int = 0
    #: (workspace, variant) pairs the batches covered.
    driver_rows: int = 0
    complete: bool = True
    #: The keyset cursor after the last committed batch, as strings.
    last_key: tuple[str, str] | None = None
    #: ``False`` when ``rollup_completion`` is not migrated yet — the run
    #: is still correct, just not resumable.
    checkpoint_available: bool = False
    #: The cursor this run started from, when it resumed a partial day.
    resumed_from: tuple[str, str] | None = None


def run_daily_rollup(
    session: Session,
    *,
    target_date: date_type | None = None,
    dry_run: bool = False,
    batch_limit: int = ROLLUP_BATCH_LIMIT,
    max_batches: int | None = None,
    deadline: datetime | None = None,
    restart: bool = False,
    commit: bool = True,
    now: datetime | None = None,
) -> RunReport:
    """Upsert one ``variant_price_daily_rollups`` row per (workspace,
    variant) that had >=1 observation on ``target_date`` (contracts/
    daily-rollup.md). ``target_date`` defaults to yesterday UTC
    (:func:`default_target_date`).

    **Set-based, batched, resumable** (EPA C7, plan F12). The day is
    walked in keyset batches of ``batch_limit`` ``(workspace_id,
    product_variant_id)`` pairs; each batch is ONE statement
    (:func:`app_shared.maintenance.rollup_sql.rollup_batch_stmt`) that
    selects the batch, collapses the day's observations to the latest
    eligible one per ``(workspace, variant, match)``, aggregates, joins
    ``variant_price_states`` and upserts — replacing the ``1 + 3N``
    statements per day this function used to issue. After each batch the
    durable ``rollup_completion`` checkpoint is written **in the same
    transaction** and the transaction is committed, so:

    * a run killed at the Celery ``time_limit`` keeps every batch it
      finished and the next invocation resumes from the cursor rather
      than re-walking the day;
    * a crash mid-batch loses that batch's rows AND its cursor advance
      together, so the batch is simply redone — and because the upsert
      is absolute (``ON CONFLICT ... DO UPDATE`` with computed values,
      never ``count + delta``) redoing it converges on the same rows.
      Nothing is ever double-counted.

    A day whose checkpoint already says ``complete`` is a no-op: the
    function returns immediately with ``rollups_upserted=0``. Pass
    ``restart=True`` (what :func:`recompute_window` does) to clear that
    and re-derive the day in full.

    ``max_batches``/``deadline`` bound one invocation. ``deadline`` is a
    tz-aware UTC instant checked *between* batches — the caller sets it
    below the Celery ``time_limit`` so the process stops itself cleanly
    instead of being SIGKILLed mid-transaction. Either bound stopping the
    run leaves ``report.complete is False``, which is the caller's signal
    to re-enqueue.

    A variant with observations that day but **no** SPEC-09
    ``variant_price_states`` row yet (never computed) has no
    ``client_price`` to snapshot (NOT NULL column) — reported in
    ``variants_skipped_no_state`` and skipped rather than erroring or
    writing a placeholder; this is distinct from the *zero-comparable*
    case (FR-013), which still gets a row (the statement's ``LEFT JOIN``
    to the aggregate, ``COALESCE(..., 0)``).

    ``dry_run=True`` (SPEC-15 Task 1.4, `scripts/backfill_daily_rollups.py`)
    renders the same statement with the data-modifying CTE removed: it
    runs the identical driver scan, collapse, aggregation and state join
    and still reports what WOULD be written, but no write statement is
    ever built or sent — not the upsert, not the checkpoint — so the
    caller's session is genuinely, Postgres-enforceably read-only for the
    whole call (a `SET TRANSACTION READ ONLY` guard holds only if no
    write is attempted; executing the real upsert and rolling back after
    is not compatible with it). A dry run also never consults the
    checkpoint: it always walks the whole day, because "what would a full
    run write" is the question it is asked.
    """
    if target_date is None:
        target_date = default_target_date(now or datetime.now(timezone.utc))
    if batch_limit <= 0:
        raise ValueError(f"batch_limit must be positive, got {batch_limit}")

    day_start, day_end = utc_day_bounds(target_date)
    report = RunReport(complete=False)

    # A dry run must send no write at all, so it neither reads nor writes
    # the checkpoint and always starts from the beginning of the day.
    checkpoint = (not dry_run) and completion_store_available(session)
    report.checkpoint_available = checkpoint
    if not checkpoint and not dry_run:
        logger.warning(
            "%s table=%s target_date=%s remedy=%s",
            EVENT_COMPLETION_STORE_ABSENT,
            "rollup_completion",
            target_date.isoformat(),
            "apply the pending rollup_completion migration; until then every "
            "invocation restarts the day from the beginning and a run killed "
            "at the Celery time_limit makes no durable forward progress",
        )

    last_workspace_id = MIN_UUID
    last_variant_id = MIN_UUID
    stamp = now or datetime.now(timezone.utc)

    if checkpoint:
        if restart:
            clear_completion(session, target_date, now=stamp)
        else:
            existing = read_completion(session, target_date)
            if existing is not None:
                if existing.complete:
                    report.complete = True
                    return report
                if existing.last_key_workspace_id is not None:
                    last_workspace_id = existing.last_key_workspace_id
                    last_variant_id = existing.last_key_variant_id or MIN_UUID
                    report.resumed_from = (str(last_workspace_id), str(last_variant_id))

    while True:
        # Cross-tenant statement (research R9) -- the one unscoped read in
        # this module; every join inside it pairs workspace_id with
        # product_variant_id, so no row is ever matched across workspaces
        # (FR-014).
        row = session.execute(  # noqa: workspace-scope
            rollup_batch_stmt(
                target_date,
                day_start,
                day_end,
                last_workspace_id=last_workspace_id,
                last_product_variant_id=last_variant_id,
                batch_limit=batch_limit,
                dry_run=dry_run,
            )
        ).first()

        driver_rows = int(row.driver_rows or 0) if row is not None else 0
        report.batches += 1
        report.driver_rows += driver_rows
        report.rollups_upserted += int(row.rollups_upserted or 0) if row is not None else 0
        if row is not None and row.variants_skipped_no_state:
            report.variants_skipped_no_state.extend(row.variants_skipped_no_state)
        if row is not None and row.last_workspace_id is not None:
            last_workspace_id = row.last_workspace_id
            last_variant_id = row.last_product_variant_id
            report.last_key = (str(last_workspace_id), str(last_variant_id))

        # A short batch means the keyset window ran off the end of the
        # day: there is nothing after the cursor, so the day is done.
        day_done = driver_rows < batch_limit

        if checkpoint:
            upsert_completion(
                session,
                target_date,
                last_workspace_id=None if last_workspace_id == MIN_UUID else last_workspace_id,
                last_variant_id=None if last_variant_id == MIN_UUID else last_variant_id,
                complete=day_done,
                now=stamp,
            )
        if commit and not dry_run:
            session.commit()

        if day_done:
            report.complete = True
            return report
        if max_batches is not None and report.batches >= max_batches:
            return report
        # Real clock, never the caller's `now` stamp: `now` exists to make
        # the WRITES deterministic in a test, and reusing it here would
        # freeze the budget and loop forever.
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            return report


# --- Durable watermark: bounded catch-up + recompute (EPA W5.5-L2 Item A) ---


#: Conservative fallbacks used when a caller passes no ``Settings``
#: (a pure unit test, a script). They mirror the shipped
#: ``Settings.ROLLUP_*`` defaults -- see ``app_shared.config``.
_DEFAULT_BACKFILL_MAX_DAYS = 7
_DEFAULT_SEED_LAG_DAYS = 1


def plan_backfill_days(
    last_complete_date: date_type,
    latest_complete_day: date_type,
    max_days: int,
) -> list[date_type]:
    """The **bounded** list of UTC days still owed, oldest first.

    ``[last_complete_date + 1 day, latest_complete_day]``, truncated to at
    most ``max_days`` entries -- so a catch-up after arbitrarily long
    downtime processes a bounded batch per invocation and makes bounded
    progress on each subsequent one, instead of one unbounded scan that
    grows with the outage and times out (or OOMs) forever after.

    Pure: no session, no clock, no settings -- the entire stepping policy
    in one testable function. Returns ``[]`` when the cursor is already at
    (or past) ``latest_complete_day``, which is the steady state, and also
    when ``max_days <= 0`` (a deliberate operator freeze).
    """
    if max_days <= 0:
        return []
    days: list[date_type] = []
    cursor = last_complete_date + timedelta(days=1)
    while cursor <= latest_complete_day and len(days) < max_days:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


@dataclass
class CatchupReport:
    """Structured summary of one :func:`run_rollup_catchup` invocation.

    ``watermark_available`` is the capability flag: ``False`` means the
    ``rollup_watermarks`` table is not migrated yet and this run fell back
    to the pre-watermark behaviour (roll up ``latest_complete_day``, once).
    ``days_remaining`` is how many owed days this run did NOT reach
    because ``max_days`` capped the batch -- the operator's "am I still
    behind?" number, and the thing that must trend to 0.

    ``complete`` is ``False`` when the run stopped with work still owed
    -- either a day did not finish inside this invocation's batch/time
    budget (``incomplete_day``) or ``max_days`` capped the batch. The
    Celery task re-enqueues itself while it is ``False`` (EPA C7).
    """

    days_processed: list[str] = field(default_factory=list)
    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)
    watermark_available: bool = False
    seeded: bool = False
    days_remaining: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    complete: bool = True
    #: The UTC day this run stopped part-way through, if any. Its
    #: watermark is deliberately NOT advanced.
    incomplete_day: str | None = None
    batches: int = 0


def run_rollup_catchup(
    session: Session,
    *,
    now_utc: datetime | None = None,
    max_days: int | None = None,
    seed_lag_days: int | None = None,
    watermark_key: str = WATERMARK_DAILY_ROLLUP,
    commit: bool = True,
    batch_limit: int = ROLLUP_BATCH_LIMIT,
    deadline: datetime | None = None,
) -> CatchupReport:
    """Roll up every UTC day owed since the durable watermark, in a bounded batch.

    This is the crash-safe replacement for "call
    :func:`run_daily_rollup` with no arguments and hope the wall clock
    never moved on without us" (see
    :mod:`app_shared.maintenance.rollup_watermark` for the failure mode).

    Per owed day, in order, oldest first:

    1. :func:`run_daily_rollup` for that day (the unchanged aggregation);
    2. :func:`~app_shared.maintenance.rollup_watermark.advance_watermark`
       for that day -- **in the same transaction**;
    3. ``session.commit()`` (unless ``commit=False``, for tests that want
       to inspect one transaction's statements).

    That order is the whole no-loss/no-double-count property. A crash
    anywhere before the commit leaves neither the day's rollup rows nor
    the advance durable, so the day is re-planned on the next run; a
    crash after it leaves both durable, so the day is not re-planned. And
    because a re-run is idempotent by construction (``ON CONFLICT ... DO
    UPDATE`` with absolute values, never ``count + delta``), even a
    torn-looking retry converges on the same rows.

    Since EPA C7 :func:`run_daily_rollup` itself commits per keyset
    batch, so the final batch of a day commits just before step 2 rather
    than with it. The property is unchanged and is now enforced twice: a
    crash in that gap leaves the day's rows durable but its watermark
    un-advanced, so the day is re-planned -- and its ``rollup_completion``
    row already says ``complete``, so the re-plan is a no-op that just
    advances the cursor. A day that did NOT finish inside this
    invocation's budget does **not** get its watermark advanced at all;
    the loop stops there, reports ``complete=False`` and names the day in
    ``incomplete_day``, and the caller re-enqueues.

    ``deadline`` (a tz-aware UTC instant) and ``batch_limit`` are passed
    straight through to :func:`run_daily_rollup`; the deadline is checked
    between batches AND between days.

    **Degraded mode.** When ``rollup_watermarks`` does not exist yet (the
    migration is pending -- see the module docstring of
    :mod:`app_shared.maintenance.rollup_watermark`), this logs
    ``rollup_watermark_store_absent`` once and rolls up exactly
    ``default_target_date(now_utc)``: byte-for-byte the behaviour every
    caller has today, no better and no worse.

    **First use.** A cursor that does not exist yet is seeded at
    ``latest_complete_day - seed_lag_days`` (default 1), so the first run
    after the migration does exactly one day rather than attempting to
    walk the whole history. Deliberate historical backfill is
    :func:`recompute_window`'s job, not a side effect of turning the
    cursor on.
    """
    now = now_utc or datetime.now(timezone.utc)
    latest_complete_day = default_target_date(now)
    batch_cap = _DEFAULT_BACKFILL_MAX_DAYS if max_days is None else max_days
    lag = _DEFAULT_SEED_LAG_DAYS if seed_lag_days is None else seed_lag_days

    report = CatchupReport()

    if not watermark_store_available(session):
        logger.warning(
            "%s table=rollup_watermarks target_date=%s remedy=%s",
            EVENT_WATERMARK_STORE_ABSENT,
            latest_complete_day.isoformat(),
            "apply the pending rollup_watermarks migration; until then a daily "
            "rollup missed during downtime is never re-attempted and its source "
            "partition can be dropped by retention before it is ever aggregated",
        )
        day_report = run_daily_rollup(
            session,
            target_date=latest_complete_day,
            batch_limit=batch_limit,
            deadline=deadline,
            commit=commit,
        )
        if commit:
            session.commit()
        report.days_processed.append(latest_complete_day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
        report.batches += day_report.batches
        if not day_report.complete:
            report.complete = False
            report.incomplete_day = latest_complete_day.isoformat()
        return report

    report.watermark_available = True

    watermark = read_watermark(session, watermark_key)
    if watermark is None:
        seed_watermark(
            session,
            latest_complete_day - timedelta(days=max(0, lag)),
            key=watermark_key,
            now=now,
        )
        if commit:
            session.commit()
        report.seeded = True
        watermark = read_watermark(session, watermark_key)
        if watermark is None:  # pragma: no cover - only if the seed was rolled back
            return report

    report.watermark_before = watermark.last_complete_date.isoformat()
    report.watermark_after = watermark.last_complete_date.isoformat()

    owed = plan_backfill_days(watermark.last_complete_date, latest_complete_day, batch_cap)
    total_owed = (latest_complete_day - watermark.last_complete_date).days
    report.days_remaining = max(0, total_owed - len(owed))

    if report.days_remaining:
        report.complete = False

    for day in owed:
        day_report = run_daily_rollup(
            session,
            target_date=day,
            batch_limit=batch_limit,
            deadline=deadline,
            commit=commit,
        )
        report.days_processed.append(day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
        report.batches += day_report.batches

        if not day_report.complete:
            # The day is only PARTLY rolled up. Advancing the watermark
            # here would declare it finished and let retention drop its
            # source partition -- the exact data loss the cursor exists
            # to prevent. Stop, report, and let the caller re-enqueue:
            # the durable `rollup_completion` cursor resumes this day
            # where it stopped.
            report.complete = False
            report.incomplete_day = day.isoformat()
            return report

        # Same transaction as the day's own upserts -- see the ordering
        # contract in `app_shared.maintenance.rollup_watermark`.
        advance_watermark(session, day, key=watermark_key, now=now)
        if commit:
            session.commit()
        report.watermark_after = day.isoformat()

        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            # Out of budget between days: whatever is still owed stays
            # owed, and the caller re-enqueues.
            remaining = (latest_complete_day - day).days
            if remaining > 0:
                report.days_remaining = remaining
                report.complete = False
            return report

    return report


@dataclass
class RecomputeReport:
    """Structured summary of one :func:`recompute_window` invocation."""

    days_recomputed: list[str] = field(default_factory=list)
    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)
    dry_run: bool = False


def recompute_window(
    session: Session,
    start_date: date_type,
    end_date: date_type,
    *,
    dry_run: bool = False,
    commit: bool = True,
) -> RecomputeReport:
    """Re-derive ``[start_date, end_date]`` (inclusive) from source observations.

    The documented, tested recompute procedure. Safe to run against live
    rollups while the normal cadence is running, because it is
    **idempotent and absolute**, not additive: each day is re-aggregated
    from ``price_observations`` and written with ``ON CONFLICT
    (workspace_id, product_variant_id, date) DO UPDATE`` setting every
    column to the freshly computed value. Re-running a day that the
    cadence already rolled up -- or that a previous recompute already
    rewrote -- overwrites the row in place. There is no counter to
    double-count.

    It deliberately **never advances or rewinds the watermark**. The
    watermark means "the sweep has reached here", which is a statement
    about the *cadence*, not about any one operator-initiated repair; and
    the advance is ``GREATEST``-guarded anyway, so even a mistaken call
    could not rewind the cadence's progress.

    One transaction per keyset batch (committed as it completes, unless
    ``commit=False``) so a long range makes durable partial progress and
    an interruption costs at most the batch in flight -- which, being
    idempotent, is simply redone.

    Every day is recomputed with ``restart=True``: a deliberate repair
    must re-derive a day even when its ``rollup_completion`` row already
    says ``complete``, which is exactly the case a repair is called for.
    That resets the day's checkpoint first, so an interrupted recompute
    resumes mid-day on the next call rather than starting over -- and
    leaves the day marked incomplete until it finishes, which is the
    honest state for C8's retention gate to see while a repair is in
    flight.

    ``dry_run=True`` forwards to :func:`run_daily_rollup`'s own genuinely
    read-only path (no write statement is ever sent), matching
    ``scripts/backfill_daily_rollups.py``'s default posture.

    Raises ``ValueError`` when ``end_date < start_date`` -- an inverted
    range is a caller bug, not an empty repair.
    """
    if end_date < start_date:
        raise ValueError(
            f"recompute_window: end_date {end_date.isoformat()} precedes "
            f"start_date {start_date.isoformat()}"
        )

    report = RecomputeReport(dry_run=dry_run)
    day = start_date
    while day <= end_date:
        day_report = run_daily_rollup(
            session,
            target_date=day,
            dry_run=dry_run,
            restart=True,
            commit=commit,
        )
        if commit and not dry_run:
            session.commit()
        report.days_recomputed.append(day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
        day += timedelta(days=1)

    return report
