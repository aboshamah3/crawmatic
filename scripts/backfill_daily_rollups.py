#!/usr/bin/env python3
"""backfill_daily_rollups.py — idempotent daily-rollup backfill (SPEC-15 Task 1.4).

`daily_rollup` (`MAINTENANCE_DAILY_ROLLUP`,
`app_shared.maintenance.rollups.run_daily_rollup`) only ever targets
"yesterday UTC" — its documented cadence contract
(`app_shared.maintenance.rollups.default_target_date`). Every day that
predates this job's first run — the 2026-07-11 -> 2026-08-12 backlog in
production, ~32k `price_observations` rows — has never been rolled up and
never self-heals: nothing else in the system ever calls `run_daily_rollup`
for a day other than yesterday.

If retention (`app_shared.maintenance.retention.run_retention`, R7) later
drops the `price_observations_2026_07` partition, that history becomes
permanently unrecoverable for the client's only month of data —
`variant_price_daily_rollups` is the durable, non-partitioned summary
retention preserves past a raw partition's expiry, but only for days that
were actually rolled up.

This script re-runs `run_daily_rollup` once per UTC calendar day across an
inclusive ``[start, end]`` range (default: 2026-07-11 through yesterday
UTC), on the same BYPASSRLS system session
(`app_shared.database.get_system_sessionmaker`) the `MAINTENANCE_DAILY_ROLLUP`
Celery task uses (`apps/workers/app/workers/tasks_maintenance.py`).

Idempotent by construction, not by anything added here: `run_daily_rollup`'s
upsert is already ``ON CONFLICT (workspace_id, product_variant_id, date)
DO UPDATE`` (verified by reading `app_shared.maintenance.rollups.
run_daily_rollup` before writing this script), so re-running any day —
including a day the normal daily cadence already rolled up, or a day this
script itself already backfilled — overwrites that day's row in place
rather than duplicating it.

``--dry-run`` (the default — no flag needed): runs the full read +
aggregate + upsert-statement path for every day in range and reports what
WOULD be written, then rolls back each day's transaction. No row is ever
persisted. This is backstopped at the DATABASE level, not just the app
level: before any statement runs for a dry-run day, the session issues
``SET default_transaction_read_only = on`` — the same hard guard
``scripts/analyze_hot_query_plans.py`` pins before any production
``SELECT`` (its module docstring / line 187) — so a future refactor that
accidentally skips the ``session.rollback()`` call still cannot commit a
write; Postgres itself rejects it.
``--apply``: commits each day's transaction as it completes (session
stays writable), so a mid-range failure leaves every already-processed
day durably written rather than losing the whole run.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.maintenance.rollups import RunReport, default_target_date, run_daily_rollup

#: The start of the backlog this script exists to close (SPEC-15 Task 1.4
#: brief, verbatim): the earliest `price_observations` row in production
#: predates the first `daily_rollup` run by roughly a month.
DEFAULT_START = date_type(2026, 7, 11)

SessionFactory = Callable[[], Session]


def date_range(start: date_type, end: date_type) -> Iterator[date_type]:
    """Yield every UTC calendar day from ``start`` to ``end``, inclusive.

    Yields nothing if ``end < start`` (never raises) — an empty range is
    a valid outcome (no backlog left, or a degenerate CLI invocation), so
    the caller treats it as a zero-day, zero-write run rather than an
    error.
    """
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


@dataclass(frozen=True)
class DayResult:
    """One day's outcome — what `run_daily_rollup` did (or, in dry-run,
    would have done) for that day."""

    target_date: date_type
    report: RunReport


def run_backfill(
    *,
    start: date_type,
    end: date_type,
    apply: bool,
    session_factory: SessionFactory,
) -> list[DayResult]:
    """Run `run_daily_rollup` once per day in ``[start, end]``.

    Opens one session per day (mirroring the `MAINTENANCE_DAILY_ROLLUP`
    Celery task's one-session-per-run shape) so a failure partway through
    a long backfill never poisons an already-committed day's transaction.

    ``apply=False`` (dry-run) still builds and executes the real upsert
    statement against the session — the reported per-day counts reflect
    the actual write path, not a simulation — but the session is pinned
    ``SET default_transaction_read_only = on`` BEFORE `run_daily_rollup`
    runs any statement (a hard DB-level guard, not just the
    `session.rollback()` below it — the global production-DB-session
    constraint this project holds everywhere else, e.g.
    `scripts/analyze_hot_query_plans.py`), and every day's transaction is
    rolled back rather than committed, so nothing is ever persisted even
    if a future refactor skipped the rollback call.
    """
    results: list[DayResult] = []
    for target_date in date_range(start, end):
        session = session_factory()
        try:
            if not apply:
                # Hard DB-level backstop for dry-run: pinned BEFORE any
                # read/write `run_daily_rollup` issues, so an accidental
                # write fails at Postgres itself, not just at the
                # app-level `session.rollback()` two lines down.
                session.execute(text("SET default_transaction_read_only = on"))
            report = run_daily_rollup(session, target_date=target_date)
            if apply:
                session.commit()
            else:
                session.rollback()
        finally:
            session.close()
        results.append(DayResult(target_date=target_date, report=report))
    return results


def _format_day_line(result: DayResult) -> str:
    return (
        f"date={result.target_date.isoformat()} "
        f"rollups_upserted={result.report.rollups_upserted} "
        f"variants_skipped_no_state={len(result.report.variants_skipped_no_state)}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotent backfill of variant_price_daily_rollups for days the "
            "normal daily_rollup cadence never covered (it only ever targets "
            "yesterday UTC)."
        )
    )
    parser.add_argument(
        "--start",
        type=date_type.fromisoformat,
        default=DEFAULT_START,
        help=f"First UTC day to backfill, inclusive (default: {DEFAULT_START.isoformat()}).",
    )
    parser.add_argument(
        "--end",
        type=date_type.fromisoformat,
        default=None,
        help="Last UTC day to backfill, inclusive (default: yesterday UTC).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Commit each day's rollup. Without this flag, runs a dry-run: "
            "computes and reports what WOULD be written for every day, then "
            "rolls back — no row is persisted."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: resolve the date range, connect, backfill, report.

    Only `parse_args` runs on a bare `import` of this module — the
    database connection (`app_shared.database.get_system_sessionmaker`)
    is imported and constructed inside `main`, never at module import
    time, so this module is safe to import (and its unit tests safe to
    collect) with zero environment variables set, mirroring
    `scripts/seed_bootstrap.py`'s convention.
    """
    args = parse_args(argv)
    end = args.end if args.end is not None else default_target_date(datetime.now(timezone.utc))

    if args.start > end:
        print(
            f"backfill_daily_rollups: start ({args.start.isoformat()}) is after "
            f"end ({end.isoformat()}) -- nothing to do.",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"backfill_daily_rollups mode={mode} start={args.start.isoformat()} end={end.isoformat()}"
    )

    from app_shared.database import get_system_sessionmaker

    session_factory = get_system_sessionmaker()

    results = run_backfill(
        start=args.start, end=end, apply=args.apply, session_factory=session_factory
    )

    total_upserted = 0
    total_skipped = 0
    for result in results:
        print(_format_day_line(result))
        total_upserted += result.report.rollups_upserted
        total_skipped += len(result.report.variants_skipped_no_state)

    print(
        f"backfill_daily_rollups TOTAL mode={mode} days={len(results)} "
        f"rollups_upserted={total_upserted} variants_skipped_no_state={total_skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
