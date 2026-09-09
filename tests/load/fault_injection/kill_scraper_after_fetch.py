#!/usr/bin/env python3
"""kill_scraper_after_fetch.py — EPA D2 row 2: scraper killed mid-batch,
targets released by the reaper (audit §13 Durability; this is the
2026-09-03 G2 deploy-survival proof, re-run here — it only became
meaningful once EPA A5 made the ``STARTED`` transition real: before A5,
``mark_targets_started`` never actually wrote ``STARTED``, so the
``STARTED`` bucket the reaper reaps was always empty and this scenario
could not have failed loudly even if the reaper were broken).

## What this does, in staging, when run for real

1. Enqueue a job with a target batch big enough to still be mid-flight a
   few seconds in.
2. Poll ``scrape_job_targets`` until a batch is observed ``STARTED``
   (``app_shared.jobs.targets`` is the single writer of that
   transition).
3. ``docker kill -s KILL`` the ``scrapers`` (or ``scrapers-browser``)
   container that claimed them, mid-fetch — no graceful shutdown, no
   chance to write a terminal status.
4. Wait past ``SCRAPE_STARTED_REAP_AFTER_SECONDS`` and trigger one pass
   of ``reap_stale_targets`` (``apps/workers/app/workers/tasks_jobs.py``)
   — pass 1, ``revert_stale_started_targets``.
5. Read the target set back and count how many of the ones that were
   ``STARTED`` under the killed container are now ``PENDING`` again with
   their dispatch stamps cleared (released back to the ordinary
   dispatcher), versus how many are still wedged ``STARTED``.

## The measurement

``targets_released_by_reaper`` — count of previously-``STARTED`` targets
now back in ``PENDING`` with ``dispatched_at IS NULL``. Pass bar: equals
``targets_started_under_killed_container`` (every one of them released;
none left permanently wedged).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    StagingGuardRefusal,
    add_staging_arguments,
    refuse_and_exit,
    require_staging,
)

__all__ = [
    "TargetRecord",
    "ReaperMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class TargetRecord:
    target_id: str
    #: Status when the container was killed (always "STARTED" for a
    #: target this script tracks — read before the kill).
    status_at_kill: str
    #: Status after the reap pass ran.
    status_after_reap: str
    dispatched_at_cleared: bool


@dataclass(frozen=True)
class ReaperMeasurement:
    targets_started_under_killed_container: int
    targets_released_by_reaper: int
    targets_still_wedged: int

    @property
    def passed(self) -> bool:
        return (
            self.targets_started_under_killed_container > 0
            and self.targets_released_by_reaper == self.targets_started_under_killed_container
            and self.targets_still_wedged == 0
        )


def compute_measurement(records: Sequence[TargetRecord]) -> ReaperMeasurement:
    started = [r for r in records if r.status_at_kill == "STARTED"]
    released = [
        r
        for r in started
        if r.status_after_reap == "PENDING" and r.dispatched_at_cleared
    ]
    wedged = [r for r in started if r.status_after_reap == "STARTED"]
    return ReaperMeasurement(
        targets_started_under_killed_container=len(started),
        targets_released_by_reaper=len(released),
        targets_still_wedged=len(wedged),
    )


def format_report(measurement: ReaperMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "kill_scraper_after_fetch: "
        f"targets_started_under_killed_container={measurement.targets_started_under_killed_container} "
        f"targets_released_by_reaper={measurement.targets_released_by_reaper} "
        f"targets_still_wedged={measurement.targets_still_wedged} "
        "pass_bar='released == started_under_killed_container and still_wedged == 0' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> list[TargetRecord]:
    return [
        TargetRecord(f"target-{i}", "STARTED", "PENDING", dispatched_at_cleared=True)
        for i in range(5)
    ]


def _fetch_live(args: argparse.Namespace) -> list[TargetRecord]:
    """Real staging query — never invoked in this EPA run (Step 1 deferred).

    Reads the target ids captured at kill time (``--target-ids-file``,
    one uuid per line, written by the operator when the kill was issued)
    and compares their state before/after the reap pass.
    """
    from sqlalchemy import create_engine, text

    if not args.target_ids_file:
        raise SystemExit("--target-ids-file is required for a live run")

    target_ids = [
        line.strip()
        for line in Path(args.target_ids_file).read_text().splitlines()
        if line.strip()
    ]
    if not target_ids:
        raise SystemExit(f"{args.target_ids_file} contains no target ids")

    engine = create_engine(args.database_url)
    query = text(
        """
        SELECT id, status, dispatched_at
        FROM scrape_job_targets
        WHERE id = ANY(:ids)
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"ids": target_ids}).mappings().all()
    engine.dispose()

    return [
        TargetRecord(
            target_id=str(row["id"]),
            status_at_kill="STARTED",  # by construction: only STARTED ids are captured
            status_after_reap=row["status"],
            dispatched_at_cleared=row["dispatched_at"] is None,
        )
        for row in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True)
    parser.add_argument(
        "--target-ids-file",
        default=None,
        help="Path to a file of target uuids (one per line) captured STARTED at kill time.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_database=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    records = _dry_run_fixture() if args.dry_run else _fetch_live(args)
    measurement = compute_measurement(records)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
