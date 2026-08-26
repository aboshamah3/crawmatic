"""Durable rollup progress watermark (EPA W5.5-L2, Item A).

## The gap this closes

``MAINTENANCE_DAILY_ROLLUP`` (``apps/workers/app/workers/
tasks_maintenance.py::daily_rollup`` ->
:func:`app_shared.maintenance.rollups.run_daily_rollup`) has exactly one
notion of "which window am I rolling up": :func:`~app_shared.maintenance.
rollups.default_target_date`, i.e. *yesterday UTC*, computed fresh from
the wall clock on every invocation. Nothing anywhere records which days
have actually been rolled up.

That makes the daily rollup **silently lossy across downtime**. The
cadence claim (``maintenance_cadences``,
:mod:`app_shared.maintenance.cadence`) is a deadline, not a cursor: when
a claim succeeds it pushes ``next_due_at`` a full interval into the
future regardless of whether the enqueued task ever ran, and when the
worker (or the whole deployment) is down for three days the three days
that elapsed are simply never anybody's ``default_target_date`` again.
The observed shape of exactly this failure is already recorded in
``scripts/backfill_daily_rollups.py``: a 2026-07-11 -> 2026-08-12
backlog that "never self-heals -- nothing else in the system ever calls
``run_daily_rollup`` for a day other than yesterday", which becomes
*permanently* unrecoverable once ``run_retention`` drops the
``price_observations`` partition the day lived in.

A watermark whose value lives in Postgres cannot be reset by a restart,
and cannot be skipped past by a wall clock that moved on without the
work being done: the cursor is a stored **date**, not elapsed process
time.

## Shape: global, no RLS, one row per cursor key

Deliberately **no** ``workspace_id`` and **no** RLS -- the same shape,
and for the same reason, as ``maintenance_cadences`` (see
:mod:`app_shared.models.maintenance_cadence`): a daily rollup is a
platform-level, cross-tenant sweep over one UTC calendar day. One day is
one window for the whole deployment; there is no per-tenant cursor to
keep and no tenant CRUD surface for one.

## No ORM model, no migration in this change

The table does not exist yet. This change is on a branch whose migration
lane is serialized and held by another task, so the DDL is **not** issued
here: it is written out in full as ``alembic/PENDING_MIGRATION_w55l2.py.txt``
(a ``.txt`` so Alembic ignores it) for the orchestrator to slot onto the
serialized lane, and every access below goes through raw
:func:`sqlalchemy.text` statements guarded by
:func:`watermark_store_available` -- the same ``to_regclass`` capability
probe :func:`app_shared.maintenance.partitions.table_exists` already uses
for a registered-but-absent partitioned table. Raw SQL is also this
module's neighbours' own house style: every read in
:mod:`app_shared.maintenance.rollups` is a ``text()`` statement.

**Until the migration lands, every entry point below reports "no store"
and the callers fall back to byte-for-byte the current behaviour** (roll
up yesterday, once). Nothing changes for an unmigrated database except
one WARNING line naming the missing table.

## Ordering contract (why this is crash-safe)

:func:`advance_watermark` must be issued in the **same transaction** as
the window's own upserts, and that transaction committed before the next
window starts:

* crash *before* the commit -> neither the rollup rows nor the watermark
  advance are durable; the window is re-planned on resume (no loss);
* crash *after* the commit -> both are durable; the window is not
  re-planned (no double work);
* a re-run of an already-committed window is harmless anyway --
  ``run_daily_rollup``'s upsert is ``ON CONFLICT (workspace_id,
  product_variant_id, date) DO UPDATE`` with **absolute** values, never
  ``count + delta``, so re-deriving a day from its source observations
  overwrites the row in place instead of accumulating (no double-count).

The advance is additionally ``GREATEST``-guarded server-side, so the
cursor is **monotonic**: a late/duplicated advance for an older window
can never rewind progress, and a recompute of an old window never makes
the sweep re-walk everything after it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "ROLLUP_WATERMARK_TABLE",
    "WATERMARK_DAILY_ROLLUP",
    "Watermark",
    "advance_watermark",
    "read_watermark",
    "seed_watermark",
    "watermark_store_available",
]

logger = logging.getLogger(__name__)

#: Physical relation backing every cursor. Created by
#: ``alembic/PENDING_MIGRATION_w55l2.py.txt`` (not yet applied).
ROLLUP_WATERMARK_TABLE = "rollup_watermarks"

#: Cursor key for the SPEC-15 daily rollup (``variant_price_daily_rollups``).
#: A stable string -- it is the durable identity of the cursor, so renaming
#: one restarts that cursor from its seed exactly once.
WATERMARK_DAILY_ROLLUP = "daily_rollup"

#: Structured event: the watermark table is absent, so the caller is
#: running in degraded (single-day, current-behaviour) mode.
EVENT_WATERMARK_STORE_ABSENT = "rollup_watermark_store_absent"


@dataclass(frozen=True)
class Watermark:
    """One durable cursor row.

    ``last_complete_date`` is the highest UTC day whose rollup is known to
    have been fully computed **and committed**. The next window to process
    is always ``last_complete_date + 1 day``.
    """

    key: str
    last_complete_date: date_type
    last_advanced_at: datetime | None
    advance_count: int


def _exists_stmt():
    """Build the (unexecuted) ``to_regclass`` existence probe.

    Split out so its rendered SQL / bound params can be asserted in a
    pure unit test without a live DB (the
    ``app_shared.maintenance.partitions._to_regclass_stmt`` precedent).
    """
    return text("SELECT to_regclass(:qualified_name) IS NOT NULL").bindparams(
        qualified_name=f"public.{ROLLUP_WATERMARK_TABLE}"
    )


def _read_stmt(key: str):
    """Build the (unexecuted) single-cursor read."""
    return text(
        f"""
        SELECT key, last_complete_date, last_advanced_at, advance_count
        FROM {ROLLUP_WATERMARK_TABLE}
        WHERE key = :key
        """
    ).bindparams(key=key)


def _seed_stmt(key: str, seed_date: date_type, now: datetime):
    """Build the (unexecuted) first-use seed.

    ``ON CONFLICT DO NOTHING`` -- two workers racing the first run both
    seed the same value and exactly one row survives; neither overwrites
    a cursor that has already made progress.
    """
    return text(
        f"""
        INSERT INTO {ROLLUP_WATERMARK_TABLE}
            (key, last_complete_date, last_advanced_at, advance_count, created_at, updated_at)
        VALUES (:key, :seed_date, NULL, 0, :now, :now)
        ON CONFLICT (key) DO NOTHING
        """
    ).bindparams(key=key, seed_date=seed_date, now=now)


def _advance_stmt(key: str, completed_date: date_type, now: datetime):
    """Build the (unexecuted) monotonic advance.

    ``GREATEST(last_complete_date, EXCLUDED.last_complete_date)`` is what
    makes the cursor monotonic **server-side**: a duplicated or
    out-of-order advance for an older window is accepted as a no-op
    rather than rewinding progress, so a recompute of an old day can
    never make the catch-up sweep re-walk every day after it.
    """
    return text(
        f"""
        INSERT INTO {ROLLUP_WATERMARK_TABLE}
            (key, last_complete_date, last_advanced_at, advance_count, created_at, updated_at)
        VALUES (:key, :completed_date, :now, 1, :now, :now)
        ON CONFLICT (key) DO UPDATE SET
            last_complete_date = GREATEST(
                {ROLLUP_WATERMARK_TABLE}.last_complete_date,
                EXCLUDED.last_complete_date
            ),
            last_advanced_at = EXCLUDED.last_advanced_at,
            advance_count = {ROLLUP_WATERMARK_TABLE}.advance_count + 1,
            updated_at = EXCLUDED.updated_at
        """
    ).bindparams(key=key, completed_date=completed_date, now=now)


def watermark_store_available(session: Session) -> bool:
    """Return ``True`` iff the ``rollup_watermarks`` relation exists.

    The capability check gating every other entry point in this module
    (the pending-migration contract in the module docstring). Uses
    ``to_regclass``, which returns ``NULL`` for a missing relation rather
    than raising, so probing an unmigrated database is a single cheap,
    catalog-safe statement that cannot abort the caller's transaction.
    """
    execute = getattr(session, "execute", None)
    if execute is None:
        return False
    return bool(execute(_exists_stmt()).scalar())


def read_watermark(session: Session, key: str = WATERMARK_DAILY_ROLLUP) -> Watermark | None:
    """Return the durable cursor for ``key``, or ``None`` if never seeded."""
    row = session.execute(_read_stmt(key)).first()
    if row is None:
        return None
    return Watermark(
        key=row.key,
        last_complete_date=row.last_complete_date,
        last_advanced_at=row.last_advanced_at,
        advance_count=row.advance_count,
    )


def seed_watermark(
    session: Session,
    seed_date: date_type,
    *,
    key: str = WATERMARK_DAILY_ROLLUP,
    now: datetime,
) -> None:
    """Create the cursor at ``seed_date`` if (and only if) it does not exist.

    The seed is deliberately **not** the epoch. A cursor born at the
    epoch would make the very first catch-up run try to walk every day
    since 1970; a cursor born at ``latest_complete_day - 1`` makes the
    first run do exactly the one day the current code already does, and
    every run after it follows the durable cursor. See
    ``Settings.ROLLUP_WATERMARK_SEED_LAG_DAYS``.
    """
    session.execute(_seed_stmt(key, seed_date, now))


def advance_watermark(
    session: Session,
    completed_date: date_type,
    *,
    key: str = WATERMARK_DAILY_ROLLUP,
    now: datetime,
) -> None:
    """Record that ``completed_date``'s window is fully rolled up.

    MUST be issued inside the same transaction as that window's own
    rollup upserts (see the module docstring's ordering contract) --
    this function does not commit.
    """
    session.execute(_advance_stmt(key, completed_date, now))
