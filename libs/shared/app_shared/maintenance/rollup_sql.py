"""Set-based daily-rollup SQL: one statement per keyset batch (EPA C7, F12).

This module holds the whole physical shape of the daily rollup — the
statement text, the keyset window, and the durable ``rollup_completion``
checkpoint that lets a partially-finished day resume exactly where it
stopped. :mod:`app_shared.maintenance.rollups` keeps the *semantics*
(which day, which rows are eligible, what the numbers mean) and drives
the loop; nothing else in the codebase renders this SQL.

WHY A SET-BASED STATEMENT AT ALL
--------------------------------
The previous implementation was a Python loop: one cross-tenant driver
scan, then **two statements per (workspace, variant) pair** (the client
state read and the day's observation read), then one upsert per pair.
At N pairs that is ``1 + 3N`` round trips — the textbook N+1 that
``tests/load/scenario_n1_detection.py`` was written to catch. At fleet
scale (hundreds of thousands of variants) a single day's rollup cannot
finish inside any sane Celery ``time_limit``, and because the whole day
was one unit of work, a run killed at the limit made **zero** durable
progress and started from nothing on the next attempt.

Here one statement per batch does the entire job for up to
``ROLLUP_BATCH_LIMIT`` (workspace, variant) pairs: pick the batch,
de-duplicate the day's observations, aggregate, join the client state,
upsert, and report back the counters plus the keyset cursor to continue
from. The number of round trips is ``ceil(pairs / 5000)``, not ``3N``.

LATEST **ELIGIBLE** OBSERVATION PER ``(workspace, variant, match, day)``
-----------------------------------------------------------------------
The semantic change this statement encodes (plan F12). The old
aggregation averaged *every* row of the day, so a competitor whose page
was scraped ten times in a day was counted ten times and a competitor
scraped once was counted once — the "average competitor price" was
really "average price reading", weighted by scrape frequency, which is
an artefact of the refresh schedule and not of the market. Ten cheap
readings of one competitor could drag a variant's daily average below
every price any human could have paid.

``DISTINCT ON (workspace_id, product_variant_id, match_id) ... ORDER BY
scraped_at DESC`` collapses each competitor match to its single latest
reading for the day *first*; ``MIN``/``AVG``/``MAX``/``COUNT`` then run
over one row per competitor. A competitor observed ten times counts
once, and ``comparable_competitor_count`` becomes a count of
**competitors**, which is what its name always claimed.

"Latest ELIGIBLE", not "latest, if eligible": the eligibility filter
(``success AND comparable AND price IS NOT NULL AND currency = the
client's``, FR-011) is applied INSIDE the ``DISTINCT ON`` CTE, before
the collapse. A competitor whose most recent read of the day failed (or
was a currency mismatch) therefore contributes its most recent *usable*
reading rather than dropping out of the day entirely — the alternative
silently shrinks the comparison set every time a scrape errors near
midnight.

THE MEASURED RANGE PREDICATE IS PRESERVED EXACTLY
-------------------------------------------------
``scraped_at >= :day_start AND scraped_at < :day_end`` — the half-open
range on the raw partition key that
:func:`app_shared.maintenance.rollups.utc_day_bounds` produces, kept
byte-for-byte from the pre-C7 statements. It is what prunes the scan to
the target day's monthly partition and keeps the ``(workspace_id,
scraped_at)`` index usable; a ``scraped_at::date = D`` cast defeats both
and additionally resolves "day" in the *session* timezone. See that
function's docstring for the full rationale — this module must not
"simplify" the predicate.

WHY THE UPSERT IS WRAPPED IN A DATA-MODIFYING CTE
-------------------------------------------------
The driver needs four things back from each batch: how many pairs it
covered (to know whether the day is finished), how many rows it wrote,
which variants had no ``variant_price_states`` row (FR: reported, not an
error), and the keyset cursor to resume from. Issuing those as extra
statements would re-scan the day's partition once more per batch and,
worse, would let the counters disagree with what the INSERT actually
did if anything changed in between. A data-modifying CTE
(``WITH upserted AS (INSERT ... RETURNING ...) SELECT ...``) makes all
four a projection of the *same* snapshot as the write, in one round
trip. There is still exactly one ``INSERT ... SELECT`` in the statement.

``gen_random_uuid()`` supplies the surrogate ``id``. Every other table
in this repo gets an application-generated UUIDv7 from
:func:`app_shared.ids.new_uuid7`, which a set-based insert cannot call;
the same trade-off (and the same function) is already made by
``6e4a9c8f2d10``'s ``INSERT ... SELECT`` backfills and by
``price_observations.attempt_uuid``'s server default. It is sound here
because ``id`` is a pure surrogate: the row's identity is
``(workspace_id, product_variant_id, date)``, which is the unique
constraint the upsert conflicts on, and nothing reads these rows in id
order.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "MIN_UUID",
    "ROLLUP_BATCH_LIMIT",
    "ROLLUP_COMPLETION_TABLE",
    "CompletionRow",
    "EVENT_COMPLETION_STORE_ABSENT",
    "clear_completion",
    "completion_store_available",
    "read_completion",
    "render_rollup_batch_sql",
    "rollup_batch_stmt",
    "upsert_completion",
]

logger = logging.getLogger(__name__)

#: Physical relation backing the per-day checkpoint. Created by the C7
#: Alembic revision ``b3f0c95a7d21``; C8's rollup-coverage gate reads its
#: ``complete`` column to decide whether a day may be considered rolled
#: up (and therefore whether its source partition may be dropped).
ROLLUP_COMPLETION_TABLE = "rollup_completion"

#: Structured event: the checkpoint table is absent (migration pending),
#: so the caller is running without resume support — every invocation
#: restarts the day from the beginning. Correct (the upsert is absolute,
#: not additive) but not bounded, so it is worth a WARNING.
EVENT_COMPLETION_STORE_ABSENT = "rollup_completion_store_absent"

#: Keyset window size: how many ``(workspace_id, product_variant_id)``
#: pairs one statement covers. 5,000 is the plan's number — large enough
#: that the per-statement planning cost is amortised, small enough that
#: one batch's transaction stays short and a kill loses at most one
#: batch of work.
ROLLUP_BATCH_LIMIT = 5000

#: The keyset start sentinel: the all-zero UUID sorts below every real
#: one, so "start from the beginning" needs no second statement shape.
#: No workspace or variant can legitimately carry it (both are UUIDv7,
#: whose timestamp prefix is never zero).
MIN_UUID = uuid.UUID(int=0)


# --- The batch statement --------------------------------------------------

_DRIVER_AND_AGGREGATE_CTES = """
    driver AS (
        SELECT DISTINCT ON (o.workspace_id, o.product_variant_id)
               o.workspace_id, o.product_variant_id, o.product_id
        FROM price_observations o
        WHERE o.scraped_at >= :day_start AND o.scraped_at < :day_end
          AND (o.workspace_id, o.product_variant_id)
              > (:last_workspace_id, :last_product_variant_id)
        ORDER BY o.workspace_id, o.product_variant_id, o.scraped_at DESC
        LIMIT :batch_limit
    ),
    latest_per_match AS (
        SELECT DISTINCT ON (o.workspace_id, o.product_variant_id, o.match_id)
               o.workspace_id, o.product_variant_id, o.match_id, o.price
        FROM price_observations o
        JOIN driver d
          ON d.workspace_id = o.workspace_id
         AND d.product_variant_id = o.product_variant_id
        JOIN variant_price_states s
          ON s.workspace_id = o.workspace_id
         AND s.product_variant_id = o.product_variant_id
        WHERE o.scraped_at >= :day_start AND o.scraped_at < :day_end
          AND o.success
          AND o.comparable
          AND o.price IS NOT NULL
          AND o.currency = s.currency
        ORDER BY o.workspace_id, o.product_variant_id, o.match_id, o.scraped_at DESC
    ),
    aggregated AS (
        SELECT workspace_id,
               product_variant_id,
               MIN(price) AS cheapest,
               ROUND(AVG(price), 4) AS average,
               MAX(price) AS highest,
               COUNT(*) AS comparable_count
        FROM latest_per_match
        GROUP BY workspace_id, product_variant_id
    ),
    eligible AS (
        SELECT d.workspace_id,
               d.product_variant_id,
               d.product_id,
               s.currency,
               s.client_price,
               s.latest_alert_type,
               a.cheapest,
               a.average,
               a.highest,
               COALESCE(a.comparable_count, 0) AS comparable_count
        FROM driver d
        JOIN variant_price_states s
          ON s.workspace_id = d.workspace_id
         AND s.product_variant_id = d.product_variant_id
        LEFT JOIN aggregated a
          ON a.workspace_id = d.workspace_id
         AND a.product_variant_id = d.product_variant_id
    ),
    skipped AS (
        SELECT d.product_variant_id
        FROM driver d
        LEFT JOIN variant_price_states s
          ON s.workspace_id = d.workspace_id
         AND s.product_variant_id = d.product_variant_id
        WHERE s.workspace_id IS NULL
    ),
    cursor_key AS (
        SELECT d.workspace_id, d.product_variant_id
        FROM driver d
        ORDER BY d.workspace_id DESC, d.product_variant_id DESC
        LIMIT 1
    )
"""

_UPSERT_CTE = """,
    upserted AS (
        INSERT INTO variant_price_daily_rollups (
            id, workspace_id, product_id, product_variant_id, date, currency,
            client_price, cheapest_competitor_price, average_competitor_price,
            highest_competitor_price, comparable_competitor_count,
            latest_alert_type, created_at, updated_at
        )
        SELECT gen_random_uuid(), e.workspace_id, e.product_id, e.product_variant_id,
               :target_date, e.currency, e.client_price, e.cheapest, e.average,
               e.highest, e.comparable_count, e.latest_alert_type, now(), now()
        FROM eligible e
        ON CONFLICT (workspace_id, product_variant_id, date) DO UPDATE SET
            product_id = EXCLUDED.product_id,
            currency = EXCLUDED.currency,
            client_price = EXCLUDED.client_price,
            cheapest_competitor_price = EXCLUDED.cheapest_competitor_price,
            average_competitor_price = EXCLUDED.average_competitor_price,
            highest_competitor_price = EXCLUDED.highest_competitor_price,
            comparable_competitor_count = EXCLUDED.comparable_competitor_count,
            latest_alert_type = EXCLUDED.latest_alert_type,
            updated_at = now()
        RETURNING 1 AS written
    )
"""

_REPORT_SELECT = """
    SELECT
        (SELECT count(*) FROM driver) AS driver_rows,
        (SELECT count(*) FROM {written_from}) AS rollups_upserted,
        (SELECT array_agg(product_variant_id::text) FROM skipped)
            AS variants_skipped_no_state,
        (SELECT workspace_id FROM cursor_key) AS last_workspace_id,
        (SELECT product_variant_id FROM cursor_key) AS last_product_variant_id
"""


def render_rollup_batch_sql(*, dry_run: bool = False) -> str:
    """Return the batch statement's SQL text.

    Pure and parameter-free so a unit test can assert the whole physical
    shape — the ``DISTINCT ON``, the preserved half-open range, the
    keyset window, the ``ON CONFLICT DO UPDATE`` — without a live
    database (the ``_driver_pairs_stmt`` precedent this replaces).

    ``dry_run=True`` renders the *same* CTEs with the data-modifying
    ``upserted`` CTE omitted and the would-be write count taken from
    ``eligible`` instead. No write statement exists in the rendered text
    at all, which is what makes ``SET TRANSACTION READ ONLY`` hold for a
    whole dry run — see
    :func:`app_shared.maintenance.rollups.run_daily_rollup`.
    """
    ctes = _DRIVER_AND_AGGREGATE_CTES if dry_run else _DRIVER_AND_AGGREGATE_CTES + _UPSERT_CTE
    select = _REPORT_SELECT.format(written_from="eligible" if dry_run else "upserted")
    return "WITH" + ctes + select


def rollup_batch_stmt(
    target_date: date_type,
    day_start: datetime,
    day_end: datetime,
    *,
    last_workspace_id: uuid.UUID = MIN_UUID,
    last_product_variant_id: uuid.UUID = MIN_UUID,
    batch_limit: int = ROLLUP_BATCH_LIMIT,
    dry_run: bool = False,
):
    """Build the (unexecuted) statement for ONE keyset batch.

    ``day_start``/``day_end`` are the caller's
    :func:`app_shared.maintenance.rollups.utc_day_bounds` values — passed
    in rather than recomputed here so there is exactly one definition of
    "day D" in the codebase, and so this module stays free of the
    calendar policy.

    Returns one row: ``driver_rows`` (pairs this batch covered — equal to
    ``batch_limit`` means there is more to do), ``rollups_upserted``,
    ``variants_skipped_no_state`` (a ``text[]``, ``NULL`` when none), and
    the ``(last_workspace_id, last_product_variant_id)`` cursor to pass
    to the next call.
    """
    if batch_limit <= 0:
        raise ValueError(f"batch_limit must be positive, got {batch_limit}")
    return text(render_rollup_batch_sql(dry_run=dry_run)).bindparams(
        day_start=day_start,
        day_end=day_end,
        last_workspace_id=last_workspace_id,
        last_product_variant_id=last_product_variant_id,
        batch_limit=batch_limit,
        **({} if dry_run else {"target_date": target_date}),
    )


# --- The durable per-day checkpoint ---------------------------------------


@dataclass(frozen=True)
class CompletionRow:
    """One ``rollup_completion`` row: where day ``date`` got to.

    ``complete`` is the column C8's retention gate reads: ``True`` means
    every (workspace, variant) pair with an observation that day has a
    ``variant_price_daily_rollups`` row, so the day's source partition
    may be dropped. ``False`` (or an absent row) means the day is still
    mid-flight and the partition must be retained.

    ``last_key_workspace_id``/``last_key_variant_id`` are the keyset
    cursor: the highest pair the last committed batch covered.
    """

    date: date_type
    last_key_workspace_id: uuid.UUID | None
    last_key_variant_id: uuid.UUID | None
    complete: bool
    updated_at: datetime | None


def _exists_stmt():
    """Build the (unexecuted) ``to_regclass`` existence probe.

    ``to_regclass`` returns ``NULL`` for a missing relation instead of
    raising, so probing an unmigrated database is a single cheap
    statement that cannot abort the caller's transaction — the same
    capability-probe shape
    :func:`app_shared.maintenance.rollup_watermark.watermark_store_available`
    uses.
    """
    return text("SELECT to_regclass(:qualified_name) IS NOT NULL").bindparams(
        qualified_name=f"public.{ROLLUP_COMPLETION_TABLE}"
    )


def _read_stmt(day: date_type):
    return text(
        f"""
        SELECT date, last_key_workspace_id, last_key_variant_id, complete, updated_at
        FROM {ROLLUP_COMPLETION_TABLE}
        WHERE date = :day
        """
    ).bindparams(day=day)


def _upsert_stmt(
    day: date_type,
    last_workspace_id: uuid.UUID | None,
    last_variant_id: uuid.UUID | None,
    complete: bool,
    now: datetime,
):
    """Build the (unexecuted) checkpoint upsert.

    Absolute, never additive: the cursor is set to what the batch that
    just committed actually reached. Written in the SAME transaction as
    that batch's upserts, which is the whole no-double-count property —
    a crash before the commit loses both the rows and the advance, so
    the batch is simply redone; a crash after keeps both, so it is not.
    """
    return text(
        f"""
        INSERT INTO {ROLLUP_COMPLETION_TABLE}
            (date, last_key_workspace_id, last_key_variant_id, complete,
             created_at, updated_at)
        VALUES (:day, :last_workspace_id, :last_variant_id, :complete, :now, :now)
        ON CONFLICT (date) DO UPDATE SET
            last_key_workspace_id = EXCLUDED.last_key_workspace_id,
            last_key_variant_id = EXCLUDED.last_key_variant_id,
            complete = EXCLUDED.complete,
            updated_at = EXCLUDED.updated_at
        """
    ).bindparams(
        day=day,
        last_workspace_id=last_workspace_id,
        last_variant_id=last_variant_id,
        complete=complete,
        now=now,
    )


def _clear_stmt(day: date_type, now: datetime):
    """Build the (unexecuted) "restart this day from the beginning" write.

    Used by a deliberate recompute: the cursor goes back to NULL and
    ``complete`` to ``False`` so the day is walked again in full. The row
    is not DELETEd — C8 reads ``complete``, and a *present, incomplete*
    day is a meaningfully different statement from an absent one (the
    latter also covers "never attempted").
    """
    return text(
        f"""
        INSERT INTO {ROLLUP_COMPLETION_TABLE}
            (date, last_key_workspace_id, last_key_variant_id, complete,
             created_at, updated_at)
        VALUES (:day, NULL, NULL, FALSE, :now, :now)
        ON CONFLICT (date) DO UPDATE SET
            last_key_workspace_id = NULL,
            last_key_variant_id = NULL,
            complete = FALSE,
            updated_at = EXCLUDED.updated_at
        """
    ).bindparams(day=day, now=now)


def completion_store_available(session: Session) -> bool:
    """Return ``True`` iff the ``rollup_completion`` relation exists."""
    execute = getattr(session, "execute", None)
    if execute is None:
        return False
    return bool(execute(_exists_stmt()).scalar())


def read_completion(session: Session, day: date_type) -> CompletionRow | None:
    """Return day ``day``'s checkpoint, or ``None`` if it has none yet."""
    row = session.execute(_read_stmt(day)).first()
    if row is None:
        return None
    return CompletionRow(
        date=row.date,
        last_key_workspace_id=row.last_key_workspace_id,
        last_key_variant_id=row.last_key_variant_id,
        complete=bool(row.complete),
        updated_at=row.updated_at,
    )


def upsert_completion(
    session: Session,
    day: date_type,
    *,
    last_workspace_id: uuid.UUID | None,
    last_variant_id: uuid.UUID | None,
    complete: bool,
    now: datetime,
) -> None:
    """Record where day ``day`` got to. Caller owns the transaction."""
    session.execute(
        _upsert_stmt(day, last_workspace_id, last_variant_id, complete, now)
    )


def clear_completion(session: Session, day: date_type, *, now: datetime) -> None:
    """Reset day ``day``'s checkpoint so the next run walks it in full."""
    session.execute(_clear_stmt(day, now))
