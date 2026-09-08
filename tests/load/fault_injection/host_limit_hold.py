#!/usr/bin/env python3
"""host_limit_hold.py — EPA D2 row 7: three nodes hammering one domain,
the fleet lease must hold (audit §13 Host protection; the mechanism under
test is EPA B5/F10's fleet-wide Redis admission gate —
``libs/shared/app_shared/limiter/fleet.py``'s ``admit_fleet``/
``release_fleet``/``fleet_snapshot``, keyed WITHOUT ``workspace_id`` on
purpose, see that module's docstring).

Unlike the other six scripts, this one does not read the database
afterwards — the claim under test ("the fleet-wide semaphore never lets
more than ``concurrency`` leases be live at once, no matter how many
INDEPENDENT processes contend for it") is a statement about Redis and
:func:`app_shared.limiter.fleet.admit_fleet` directly, so this script
drives the REAL function against ``--redis-url``, from three real,
independent OS processes (not three threads in one process — a thread
pool sharing one Python-level lock could accidentally serialize what
should be a genuine multi-process race), and samples
:func:`app_shared.limiter.fleet.fleet_snapshot` throughout.

## What this does, in staging, when run for real

1. Spawn three subprocesses ("nodes"), each looping: ``admit_fleet`` for
   the SAME ``(domain, transport)`` pair, hold the lease for a short
   random interval, ``release_fleet``, repeat — for
   :data:`RUN_SECONDS`.
2. A monitor loop in the parent samples ``fleet_snapshot`` every
   :data:`SAMPLE_INTERVAL_SECONDS` for the same window.
3. Compare every sample's ``in_flight`` against its ``concurrency`` cap.

## The measurement

``cap_exceeded_samples`` — count of samples where ``in_flight >
concurrency``. Pass bar: 0, for every sample, across all three nodes
contending at once.
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

NODE_COUNT = 3
RUN_SECONDS = 30
SAMPLE_INTERVAL_SECONDS = 0.25
DEFAULT_CONCURRENCY = 4
DEFAULT_RATE_PER_MINUTE = 600
LEASE_TTL_SECONDS = 5

__all__ = [
    "NODE_COUNT",
    "RUN_SECONDS",
    "SAMPLE_INTERVAL_SECONDS",
    "LeaseSample",
    "HostLimitMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class LeaseSample:
    timestamp: float
    in_flight: int
    concurrency_cap: int


@dataclass(frozen=True)
class HostLimitMeasurement:
    samples_examined: int
    nodes_contending: int
    concurrency_cap: int
    max_in_flight: int
    cap_exceeded_samples: int

    @property
    def passed(self) -> bool:
        return (
            self.samples_examined > 0
            and self.nodes_contending >= 3
            and self.cap_exceeded_samples == 0
        )


def compute_measurement(
    samples: Sequence[LeaseSample], *, nodes_contending: int
) -> HostLimitMeasurement:
    if not samples:
        return HostLimitMeasurement(0, nodes_contending, 0, 0, 0)
    cap = samples[0].concurrency_cap
    exceeded = sum(1 for s in samples if s.in_flight > s.concurrency_cap)
    return HostLimitMeasurement(
        samples_examined=len(samples),
        nodes_contending=nodes_contending,
        concurrency_cap=cap,
        max_in_flight=max(s.in_flight for s in samples),
        cap_exceeded_samples=exceeded,
    )


def format_report(measurement: HostLimitMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "host_limit_hold: "
        f"nodes_contending={measurement.nodes_contending} "
        f"samples_examined={measurement.samples_examined} "
        f"concurrency_cap={measurement.concurrency_cap} "
        f"max_in_flight={measurement.max_in_flight} "
        f"cap_exceeded_samples={measurement.cap_exceeded_samples} "
        "pass_bar='cap_exceeded_samples == 0 across >= 3 contending nodes' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> tuple[list[LeaseSample], int]:
    """A synthetic sample series shaped like a passing run: in_flight
    oscillates at or under the cap for three contending nodes."""
    cap = DEFAULT_CONCURRENCY
    pattern = [1, 2, 3, 4, 3, 2, 4, 3, 1, 0]
    samples = [
        LeaseSample(timestamp=float(i), in_flight=n, concurrency_cap=cap)
        for i, n in enumerate(pattern)
    ]
    return samples, NODE_COUNT


def _node_worker(
    redis_url: str, domain: str, transport: str, concurrency: int, rate_per_minute: int
) -> None:  # pragma: no cover - runs only in a real subprocess against real staging Redis
    import random
    import time

    import redis as redis_lib

    from app_shared.limiter.fleet import admit_fleet, release_fleet

    client = redis_lib.from_url(redis_url)
    deadline = time.monotonic() + RUN_SECONDS
    try:
        while time.monotonic() < deadline:
            lease = admit_fleet(
                client,
                domain=domain,
                transport=transport,
                rate_per_minute=rate_per_minute,
                concurrency=concurrency,
                lease_ttl=LEASE_TTL_SECONDS,
            )
            if lease is None:
                time.sleep(0.05)
                continue
            time.sleep(random.uniform(0.05, 0.3))
            release_fleet(client, lease)
    finally:
        client.close()


def _run_live(args: argparse.Namespace) -> tuple[list[LeaseSample], int]:
    """Real staging action — never invoked in this EPA run (Step 1 deferred)."""
    import multiprocessing
    import time

    import redis as redis_lib

    from app_shared.limiter.fleet import fleet_snapshot

    procs = [
        multiprocessing.Process(
            target=_node_worker,
            args=(
                args.redis_url,
                args.domain,
                args.transport,
                args.concurrency,
                args.rate_per_minute,
            ),
        )
        for _ in range(NODE_COUNT)
    ]
    for proc in procs:
        proc.start()

    client = redis_lib.from_url(args.redis_url)
    samples: list[LeaseSample] = []
    deadline = time.monotonic() + RUN_SECONDS
    try:
        while time.monotonic() < deadline:
            snapshot = fleet_snapshot(
                client,
                domain=args.domain,
                transport=args.transport,
                concurrency=args.concurrency,
            )
            samples.append(
                LeaseSample(
                    timestamp=time.time(),
                    in_flight=snapshot.in_flight,
                    concurrency_cap=snapshot.concurrency,
                )
            )
            time.sleep(SAMPLE_INTERVAL_SECONDS)
    finally:
        client.close()
        for proc in procs:
            proc.join(timeout=RUN_SECONDS + 10)

    return samples, NODE_COUNT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_redis=True)
    parser.add_argument("--domain", default="fault-injection-host-limit.example")
    parser.add_argument("--transport", default="HTTP", choices=["HTTP", "BROWSER"])
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--rate-per-minute", type=int, default=DEFAULT_RATE_PER_MINUTE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_redis=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    if args.dry_run:
        samples, nodes = _dry_run_fixture()
    else:
        samples, nodes = _run_live(args)

    measurement = compute_measurement(samples, nodes_contending=nodes)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
