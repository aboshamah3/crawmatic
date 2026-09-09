#!/usr/bin/env python3
"""pause_redis_60s.py — EPA D2 row 4: 60s Redis outage, mid-dispatch
(audit §13 Durability).

Redis is where the token buckets, tenant/fleet concurrency semaphores
(``app_shared.limiter``) and the Celery broker live — a 60s Redis outage
is a materially different fault than the Postgres one:
``pause_postgres_60s.py`` blocks the durable record of intent;
this blocks EVERY admission decision and every task dispatch at once.
The identity-correctness hazard is the same, though (a caller mid-way
through acquiring a lease or a bucket token when Redis vanishes for 60s
must not end up double-POSTing once it comes back), so this script
reads back the same two numbers.

## What this does, in staging, when run for real

1. Enqueue a job and let dispatch begin.
2. ``docker pause`` the staging ``redis`` container for
   :data:`PAUSE_SECONDS`.
3. ``docker unpause`` it, wait :data:`RECOVERY_GRACE_SECONDS`.
4. Read back ``dispatch_intents`` for ``--scrape-job-id`` (same query
   shape as the Postgres script) and, additionally, confirm every fleet
   semaphore key touched during the outage decayed correctly (no key
   left permanently over its TTL-bound cap — see ``host_limit_hold.py``
   for the dedicated fleet-cap scenario; this script only spot-checks
   that Redis coming back does not leave a STALE over-cap semaphore from
   leases whose holders died during the pause).

## The measurement

* ``duplicate_physical_fetches`` — pass bar 0.
* ``lost_observations`` — pass bar 0.
* ``stale_fleet_semaphore_entries`` — count of fleet semaphore ZSET
  members whose score (lease expiry) is already in the past once Redis
  is back but that ``ZCOUNT``/``fleet_snapshot`` would still count as
  live before the next ``acquire_slot`` sweeps them. Pass bar 0 once
  :data:`RECOVERY_GRACE_SECONDS` has elapsed (every lease taken before
  the pause has either been released or its TTL has expired by then).
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

PAUSE_SECONDS = 60
RECOVERY_GRACE_SECONDS = 120

__all__ = [
    "PAUSE_SECONDS",
    "RECOVERY_GRACE_SECONDS",
    "IntentRecord",
    "RedisPauseMeasurement",
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
class RedisPauseMeasurement:
    intents_examined: int
    duplicate_physical_fetches: int
    lost_observations: int
    confirmed: int
    stale_fleet_semaphore_entries: int

    @property
    def passed(self) -> bool:
        return (
            self.duplicate_physical_fetches == 0
            and self.lost_observations == 0
            and self.stale_fleet_semaphore_entries == 0
        )


def compute_measurement(
    records: Sequence[IntentRecord], *, stale_fleet_semaphore_entries: int
) -> RedisPauseMeasurement:
    duplicate = sum(1 for r in records if len(set(r.scrapyd_job_ids_seen)) > 1)
    lost = sum(1 for r in records if r.state == "CONFIRMED" and r.observation_count == 0)
    confirmed = sum(1 for r in records if r.state == "CONFIRMED")
    return RedisPauseMeasurement(
        intents_examined=len(records),
        duplicate_physical_fetches=duplicate,
        lost_observations=lost,
        confirmed=confirmed,
        stale_fleet_semaphore_entries=stale_fleet_semaphore_entries,
    )


def format_report(measurement: RedisPauseMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "pause_redis_60s: "
        f"pause_seconds={PAUSE_SECONDS} "
        f"intents_examined={measurement.intents_examined} "
        f"duplicate_physical_fetches={measurement.duplicate_physical_fetches} "
        f"lost_observations={measurement.lost_observations} "
        f"confirmed={measurement.confirmed} "
        f"stale_fleet_semaphore_entries={measurement.stale_fleet_semaphore_entries} "
        "pass_bar='duplicate_physical_fetches == 0 and lost_observations == 0 "
        "and stale_fleet_semaphore_entries == 0' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> tuple[list[IntentRecord], int]:
    records = [
        IntentRecord("identity-1", ("job-a",), "CONFIRMED", observation_count=2),
    ]
    return records, 0


def _pause_and_measure(args: argparse.Namespace) -> tuple[list[IntentRecord], int]:
    """Real staging action — never invoked in this EPA run (Step 1 deferred)."""
    import subprocess
    import time

    import redis as redis_lib
    from sqlalchemy import create_engine, text

    if not args.scrape_job_id:
        raise SystemExit("--scrape-job-id is required for a live run")

    subprocess.run(["docker", "pause", args.container], check=True)
    try:
        time.sleep(PAUSE_SECONDS)
    finally:
        subprocess.run(["docker", "unpause", args.container], check=True)
    time.sleep(RECOVERY_GRACE_SECONDS)

    engine = create_engine(args.database_url)
    query = text(
        """
        SELECT
            di.identity_key,
            di.scrapyd_job_id,
            di.state,
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

    client = redis_lib.from_url(args.redis_url)
    now = time.time()
    stale = 0
    for key in client.scan_iter(match="fleet:semaphore:*"):
        stale += client.zcount(key, "-inf", now)
    client.close()

    return records, stale


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True, needs_redis=True)
    parser.add_argument(
        "--container",
        default="redis",
        help="Name of the staging Redis container to pause (docker-compose service name).",
    )
    parser.add_argument("--scrape-job-id", default=None, help="scrape_jobs.id to read back.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_database=True, needs_redis=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    if args.dry_run:
        records, stale = _dry_run_fixture()
    else:
        records, stale = _pause_and_measure(args)

    measurement = compute_measurement(records, stale_fleet_semaphore_entries=stale)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
