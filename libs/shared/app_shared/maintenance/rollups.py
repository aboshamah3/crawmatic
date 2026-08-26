"""Daily rollup aggregation job (SPEC-15 US2, contracts/daily-rollup.md, research R6).

For UTC date ``D`` (default: yesterday UTC, the most-recently-COMPLETED
day — clarification), upserts one ``variant_price_daily_rollups`` row per
``(workspace_id, product_variant_id)`` that had >=1 ``price_observations``
row on ``D``:

* **Competitor min/avg/max + comparable count** are computed in Python
  from that day's raw observations (:func:`aggregate_competitor_prices`,
  a pure function — no live DB needed to unit-test it), filtered to
  ``success AND comparable AND price IS NOT NULL AND currency ==
  <client currency>`` (FR-011) — a currency mismatch (or a failed/
  unpriced/non-comparable observation) is excluded from BOTH the
  aggregate AND the count. ``comparable`` is the *persisted* SPEC-09
  decision (already false on a currency mismatch) — read, not
  recomputed (research R6).
* **``client_price``/``currency``/``latest_alert_type``/``product_id``**
  are read (not recomputed) from the SPEC-09 current-comparison surface
  ``variant_price_states``, scoped by ``workspace_id`` + ``product_variant_id``
  (R6) — the only source of client-price state (no per-day history exists).
* The upsert is keyed on ``(workspace_id, product_variant_id, date)``
  (``ON CONFLICT ... DO UPDATE``, FR-010) — idempotent re-run/backfill.

The driver scan (step 1: "which (workspace, variant) pairs had activity
on D") is inherently cross-tenant — one day spans every workspace — so
it runs unscoped on the BYPASSRLS system session (`# noqa:
workspace-scope`, research R9). Every subsequent read/write for a given
pair carries an explicit ``workspace_id=`` (FR-014).

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

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app_shared.maintenance.rollup_watermark import (
    EVENT_WATERMARK_STORE_ABSENT,
    WATERMARK_DAILY_ROLLUP,
    advance_watermark,
    read_watermark,
    seed_watermark,
    watermark_store_available,
)
from app_shared.models.rollups import VariantPriceDailyRollup

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
    or a fake row in a unit test)."""

    price: Decimal | None
    currency: str | None
    success: bool
    comparable: bool


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

    Filters to ``success AND comparable AND price IS NOT NULL AND
    currency == client_currency`` (FR-011) — a currency mismatch (already
    flagged ``comparable=False`` by SPEC-09) or any failed/unpriced/
    non-comparable row is excluded from BOTH the min/avg/max aggregate
    AND the count, never merely from the aggregate. Zero matching rows
    -> NULL ``cheapest``/``average``/``highest`` and ``comparable_count=0``
    (FR-013) — a valid result, never an error. All arithmetic is exact
    ``Decimal`` (never float); the average is quantized to the column's
    ``NUMERIC(18,4)`` scale with ``ROUND_HALF_UP`` (ties away from zero)
    since an arithmetic mean is not generally exact at 4 decimal places.
    """
    prices = [
        row.price
        for row in rows
        if row.success and row.comparable and row.price is not None and row.currency == client_currency
    ]
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


def _driver_pairs_stmt(target_date: date_type):
    """Build the (unexecuted) cross-tenant driver-scan statement.

    Distinct ``(workspace_id, product_variant_id, product_id)`` with >=1
    ``price_observations`` row on ``target_date`` (contracts/daily-rollup.md
    step 1) — inherently cross-tenant (one day spans every workspace), so
    this is the one unscoped scan in this module (`# noqa:
    workspace-scope`, research R9). Split out so its rendered SQL can be
    asserted in a pure unit test without a live DB (mirrors
    `app_shared.maintenance.partitions._to_regclass_stmt`).

    The day predicate is a half-open range on the raw partition key
    (:func:`utc_day_bounds`) so the scan prunes to the target day's
    partition instead of seq-scanning every month.
    """
    day_start, day_end = utc_day_bounds(target_date)
    return text(
        """
        SELECT DISTINCT workspace_id, product_variant_id, product_id
        FROM price_observations
        WHERE scraped_at >= :day_start AND scraped_at < :day_end
        """
    ).bindparams(day_start=day_start, day_end=day_end)


def _day_observations_stmt(
    target_date: date_type, workspace_id: uuid.UUID, product_variant_id: uuid.UUID
):
    """Build the (unexecuted) statement fetching one (ws, variant, day)'s
    raw observation rows (price/currency/success/comparable) — the
    unfiltered input to :func:`aggregate_competitor_prices`. Explicitly
    scoped by ``workspace_id`` (FR-014) in addition to the day + variant.

    This statement executes once per (workspace, variant) pair returned
    by :func:`_driver_pairs_stmt`, so its per-call cost is multiplied by
    the driver's row count — the half-open range on the raw partition
    key (:func:`utc_day_bounds`) is what keeps it an index scan of one
    partition rather than a full-table seq scan per pair.
    """
    day_start, day_end = utc_day_bounds(target_date)
    return text(
        """
        SELECT price, currency, success, comparable
        FROM price_observations
        WHERE scraped_at >= :day_start AND scraped_at < :day_end
          AND workspace_id = :workspace_id
          AND product_variant_id = :product_variant_id
        """
    ).bindparams(
        day_start=day_start,
        day_end=day_end,
        workspace_id=workspace_id,
        product_variant_id=product_variant_id,
    )


def _client_state_stmt(workspace_id: uuid.UUID, product_variant_id: uuid.UUID):
    """Build the (unexecuted) statement reading the SPEC-09 current-state
    surface (`client_price`/`currency`/`latest_alert_type`) for one
    variant — read, not recomputed (research R6). Explicitly scoped by
    ``workspace_id`` (FR-014).
    """
    return text(
        """
        SELECT client_price, currency, latest_alert_type
        FROM variant_price_states
        WHERE workspace_id = :workspace_id AND product_variant_id = :product_variant_id
        """
    ).bindparams(workspace_id=workspace_id, product_variant_id=product_variant_id)


@dataclass
class RunReport:
    """Structured summary of one ``run_daily_rollup`` run (FR-023,
    data-model.md §5) — logged by the Celery task wrapper, never
    persisted."""

    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)


def run_daily_rollup(
    session: Session, *, target_date: date_type | None = None, dry_run: bool = False
) -> RunReport:
    """Upsert one ``variant_price_daily_rollups`` row per (workspace,
    variant) that had >=1 observation on ``target_date`` (contracts/
    daily-rollup.md). ``target_date`` defaults to yesterday UTC
    (:func:`default_target_date`).

    A variant with observations that day but **no** SPEC-09
    ``variant_price_states`` row yet (never computed) has no
    ``client_price`` to snapshot (NOT NULL column) — recorded in
    ``variants_skipped_no_state`` and skipped rather than erroring or
    writing a placeholder; this is distinct from the *zero-comparable*
    case (FR-013), which still gets a row.

    ``dry_run=True`` (SPEC-15 Task 1.4, `scripts/backfill_daily_rollups.py`):
    runs every read and the full aggregation exactly as normal — the same
    driver scan, client-state read, day-observations read, and
    :func:`aggregate_competitor_prices` call — and still counts the
    result in ``report.rollups_upserted``, but the ``INSERT ... ON
    CONFLICT`` statement is never built or executed. This makes the
    caller's session genuinely, Postgres-enforceable read-only for the
    whole call (no write statement is EVER sent), which a real upsert +
    app-level ``session.rollback()`` cannot guarantee on its own — a
    transaction-level ``SET TRANSACTION READ ONLY`` only holds if no
    write statement is subsequently attempted in that same transaction;
    ``session.execute(stmt)``ing the real upsert first and rolling back
    after is NOT compatible with that guard (Postgres raises `cannot
    execute INSERT in a read-only transaction` the instant the write is
    attempted). ``dry_run=False`` (the default) is byte-for-byte the
    original behaviour — every existing caller (the `MAINTENANCE_DAILY_
    ROLLUP` Celery task, live/unit tests) is unaffected.
    """
    if target_date is None:
        target_date = default_target_date(datetime.now(timezone.utc))

    report = RunReport()

    # Cross-tenant scan (research R9) -- the one unscoped read in this
    # module; every subsequent read/write below carries an explicit
    # workspace_id= (FR-014).
    pairs = session.execute(_driver_pairs_stmt(target_date)).all()  # noqa: workspace-scope

    for row in pairs:
        workspace_id = row.workspace_id
        product_variant_id = row.product_variant_id
        product_id = row.product_id

        state_row = session.execute(
            _client_state_stmt(workspace_id, product_variant_id)
        ).first()
        if state_row is None:
            report.variants_skipped_no_state.append(str(product_variant_id))
            continue

        observation_rows = [
            ObservationRow(
                price=obs.price,
                currency=obs.currency,
                success=obs.success,
                comparable=obs.comparable,
            )
            for obs in session.execute(
                _day_observations_stmt(target_date, workspace_id, product_variant_id)
            )
        ]
        aggregate = aggregate_competitor_prices(
            observation_rows, client_currency=state_row.currency
        )

        values = {
            "workspace_id": workspace_id,
            "product_id": product_id,
            "product_variant_id": product_variant_id,
            "date": target_date,
            "currency": state_row.currency,
            "client_price": state_row.client_price,
            "cheapest_competitor_price": aggregate.cheapest,
            "average_competitor_price": aggregate.average,
            "highest_competitor_price": aggregate.highest,
            "comparable_competitor_count": aggregate.comparable_count,
            "latest_alert_type": state_row.latest_alert_type,
        }
        if not dry_run:
            stmt = pg_insert(VariantPriceDailyRollup).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "product_variant_id", "date"],
                set_={
                    "product_id": stmt.excluded.product_id,
                    "currency": stmt.excluded.currency,
                    "client_price": stmt.excluded.client_price,
                    "cheapest_competitor_price": stmt.excluded.cheapest_competitor_price,
                    "average_competitor_price": stmt.excluded.average_competitor_price,
                    "highest_competitor_price": stmt.excluded.highest_competitor_price,
                    "comparable_competitor_count": stmt.excluded.comparable_competitor_count,
                    "latest_alert_type": stmt.excluded.latest_alert_type,
                    "updated_at": func.now(),
                },
            )
            session.execute(stmt)
        # dry_run=True: `values` was fully computed (proves what WOULD be
        # written) but no statement is ever sent -- the count below is
        # the only side effect.
        report.rollups_upserted += 1

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
    """

    days_processed: list[str] = field(default_factory=list)
    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)
    watermark_available: bool = False
    seeded: bool = False
    days_remaining: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None


def run_rollup_catchup(
    session: Session,
    *,
    now_utc: datetime | None = None,
    max_days: int | None = None,
    seed_lag_days: int | None = None,
    watermark_key: str = WATERMARK_DAILY_ROLLUP,
    commit: bool = True,
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
        day_report = run_daily_rollup(session, target_date=latest_complete_day)
        if commit:
            session.commit()
        report.days_processed.append(latest_complete_day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
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

    for day in owed:
        day_report = run_daily_rollup(session, target_date=day)
        # Same transaction as the day's own upserts -- see the ordering
        # contract in `app_shared.maintenance.rollup_watermark`.
        advance_watermark(session, day, key=watermark_key, now=now)
        if commit:
            session.commit()
        report.days_processed.append(day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
        report.watermark_after = day.isoformat()

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

    One transaction per day (committed as it completes, unless
    ``commit=False``) so a long range makes durable partial progress and
    an interruption costs at most the day in flight -- which, being
    idempotent, is simply redone.

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
        day_report = run_daily_rollup(session, target_date=day, dry_run=dry_run)
        if commit and not dry_run:
            session.commit()
        report.days_recomputed.append(day.isoformat())
        report.rollups_upserted += day_report.rollups_upserted
        report.variants_skipped_no_state.extend(day_report.variants_skipped_no_state)
        day += timedelta(days=1)

    return report
