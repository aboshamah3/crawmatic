#!/usr/bin/env python3
"""pause_postgres_60s.py — EPA D2 row 3: 60s Postgres outage, mid-dispatch
(audit §13 Durability).

## What this does, in staging, when run for real

1. Enqueue a job and let dispatch begin (some intents reach ``POSTED``).
2. ``docker pause`` the staging ``postgres`` container for exactly 60
   seconds (``PAUSE_SECONDS``) — every query against it blocks, nothing
   is killed, nothing loses its connection cleanly the way a restart
   would. This is deliberately the SOFTER failure: a scheduler/worker
   loop retrying against a database that is merely slow, not gone.
3. ``docker unpause`` it and record the wall-clock moment.
4. Let the ordinary dispatch/reconcile loops run for
   ``RECOVERY_GRACE_SECONDS`` past unpause.
5. Read back the same two identity-correctness numbers
   ``kill_worker_after_post.py`` computes (this fault is a different
   trigger for the identical hazard: a caller mid-way through the
   five-step dispatch protocol when its database round-trip suddenly
   blocks for 60s must not end up POSTing twice, or losing the intent
   that would have proven a POST happened), plus how long dispatch took
   to resume producing CONFIRMED intents after unpause.

## The measurement

* ``duplicate_physical_fetches`` — pass bar 0 (identical definition to
  ``kill_worker_after_post.py``: identities where more than one distinct
  ``scrapyd_job_id`` was POSTed).
* ``lost_observations`` — pass bar 0 (identical definition).
* ``recovery_seconds`` — wall-clock from unpause to the first new
  ``CONFIRMED`` intent. Reported, not gated (no target from the plan
  text) — an operator reads it to judge whether ``DEFAULT_LEASE_SECONDS``
  / reservation lease durations comfortably cover a Postgres blip this
  long, which is exactly the operational question a 60s pause exists to
  answer.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
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

#: How long the container is paused for. Matches the row name; not
#: configurable on the CLI so the matrix's own doc always describes what
#: actually ran.
PAUSE_SECONDS = 60

#: How long to let recovery run past unpause before reading the result.
RECOVERY_GRACE_SECONDS = 120

__all__ = [
    "PAUSE_SECONDS",
    "RECOVERY_GRACE_SECONDS",
    "IntentRecord",
    "PauseMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class IntentRecord:
    identity_key: str
    scrapyd_job_ids_seen: tuple[str, ...]
    state: str
    observation_count: int


@dataclass(frozen=True)
class PauseMeasurement:
    intents_examined: int
    duplicate_physical_fetches: int
    lost_observations: int
    confirmed: int
    recovery_seconds: float | None

    @property
    def passed(self) -> bool:
        return self.duplicate_physical_fetches == 0 and self.lost_observations == 0


def compute_measurement(
    records: Sequence[IntentRecord], *, recovery_seconds: float | None
) -> PauseMeasurement:
    duplicate = sum(1 for r in records if len(set(r.scrapyd_job_ids_seen)) > 1)
    lost = sum(1 for r in records if r.state == "CONFIRMED" and r.observation_count == 0)
    confirmed = sum(1 for r in records if r.state == "CONFIRMED")
    return PauseMeasurement(
        intents_examined=len(records),
        duplicate_physical_fetches=duplicate,
        lost_observations=lost,
        confirmed=confirmed,
        recovery_seconds=recovery_seconds,
    )


def format_report(measurement: PauseMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    recovery = (
        f"{measurement.recovery_seconds:.1f}s"
        if measurement.recovery_seconds is not None
        else "unmeasured"
    )
    return (
        "pause_postgres_60s: "
        f"pause_seconds={PAUSE_SECONDS} "
        f"intents_examined={measurement.intents_examined} "
        f"duplicate_physical_fetches={measurement.duplicate_physical_fetches} "
        f"lost_observations={measurement.lost_observations} "
        f"confirmed={measurement.confirmed} "
        f"recovery_seconds={recovery} "
        "pass_bar='duplicate_physical_fetches == 0 and lost_observations == 0 "
        "(recovery_seconds is reported, not gated)' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> tuple[list[IntentRecord], float]:
    records = [
        IntentRecord("identity-1", ("job-a",), "CONFIRMED", observation_count=2),
        IntentRecord("identity-2", ("job-b",), "CONFIRMED", observation_count=1),
    ]
    return records, 4.2


def _pause_and_measure(args: argparse.Namespace) -> tuple[list[IntentRecord], float]:
    """Real staging action — never invoked in this EPA run (Step 1 deferred).

    Pauses ``args.container`` for :data:`PAUSE_SECONDS`, unpauses it,
    waits :data:`RECOVERY_GRACE_SECONDS`, then reads back
    ``dispatch_intents`` for ``--scrape-job-id`` exactly as
    ``kill_worker_after_post._fetch_live`` does, plus the first
    ``updated_at`` on a ``CONFIRMED`` row at or after the unpause
    timestamp.
    """
    import subprocess
    import time

    from sqlalchemy import create_engine, text

    if not args.scrape_job_id:
        raise SystemExit("--scrape-job-id is required for a live run")

    subprocess.run(["docker", "pause", args.container], check=True)
    try:
        time.sleep(PAUSE_SECONDS)
    finally:
        subprocess.run(["docker", "unpause", args.container], check=True)
    unpaused_at = time.time()
    time.sleep(RECOVERY_GRACE_SECONDS)

    engine = create_engine(args.database_url)
    query = text(
        """
        SELECT
            di.identity_key,
            di.scrapyd_job_id,
            di.state,
            di.updated_at,
            COALESCE(obs.observation_count, 0) AS observation_count
        FROM dispatch_intents di
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS observation_count
            FROM price_observations po
            WHERE po.scrape_job_id = di.scrape_job_id
        ) obs ON TRUE
        WHERE di.scrape_job_id = :job_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"job_id": args.scrape_job_id}).mappings().all()
    engine.dispose()

    records = [
        IntentRecord(
            identity_key=row["identity_key"],
            scrapyd_job_ids_seen=(str(row["scrapyd_job_id"]),),
            state=row["state"],
            observation_count=int(row["observation_count"]),
        )
        for row in rows
    ]
    first_confirmed_after = min(
        (
            row["updated_at"].timestamp()
            for row in rows
            if row["state"] == "CONFIRMED" and row["updated_at"].timestamp() >= unpaused_at
        ),
        default=None,
    )
    recovery_seconds = (
        first_confirmed_after - unpaused_at if first_confirmed_after is not None else None
    )
    return records, recovery_seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True)
    parser.add_argument(
        "--container",
        default="postgres",
        help="Name of the staging Postgres container to pause (docker-compose service name).",
    )
    parser.add_argument("--scrape-job-id", default=None, help="scrape_jobs.id to read back.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_database=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    if args.dry_run:
        records, recovery_seconds = _dry_run_fixture()
    else:
        records, recovery_seconds = _pause_and_measure(args)

    measurement = compute_measurement(records, recovery_seconds=recovery_seconds)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
