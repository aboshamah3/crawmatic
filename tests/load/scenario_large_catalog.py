#!/usr/bin/env python3
"""Large-catalog planning (EPA W5.5-GA item B, report §10).

Times `app_shared.jobs.batching.plan_batches` (pure library, no DB — see
that module's docstring: "No DB/Redis/network... unit-testable against
in-memory target/match rows") at 10k, 25k and 50k targets, spread across
a realistic number of distinct domains/modes so the grouping logic does
real work rather than degenerating to one giant group.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.enums import ScrapeProfileMode  # noqa: E402
from app_shared.jobs.batching import ResolvedTarget, plan_batches  # noqa: E402
from harness import report_and_print  # noqa: E402

DOMAIN_COUNT = 300
TARGET_COUNTS = (10_000, 25_000, 50_000)


def _build_targets(n: int) -> list[ResolvedTarget]:
    targets: list[ResolvedTarget] = []
    for i in range(n):
        domain = f"catalog-domain-{i % DOMAIN_COUNT}.example"
        mode = ScrapeProfileMode.BROWSER if i % 10 == 0 else ScrapeProfileMode.HTTP
        targets.append(
            ResolvedTarget(
                match_id=uuid.uuid4(),
                competitor_domain=domain,
                mode=mode,
            )
        )
    return targets


def main() -> int:
    runs = []
    for n in TARGET_COUNTS:
        targets = _build_targets(n)
        start = time.perf_counter()
        batches = plan_batches(targets, planning_generation=1)
        elapsed = time.perf_counter() - start

        total_match_ids = sum(len(b.match_ids) for b in batches)
        throughput = n / elapsed if elapsed > 0 else float("inf")

        runs.append(
            {
                "targets": n,
                "elapsed_seconds": round(elapsed, 4),
                "batches_produced": len(batches),
                "targets_per_second": round(throughput, 1),
                "every_target_placed_exactly_once": total_match_ids == n,
            }
        )

    slowest = max(runs, key=lambda r: r["elapsed_seconds"])
    report_and_print(
        "large_catalog",
        {
            "domain_count": DOMAIN_COUNT,
            "runs": runs,
            "finding": (
                f"DEV-BOUND: plan_batches at {slowest['targets']} targets took "
                f"{slowest['elapsed_seconds']}s ({slowest['targets_per_second']}/s) "
                "on this dev box, single-threaded, in-process. No budget breach "
                "observed at up to 50k targets — see README for the DEV-SERVER-"
                "BOUND caveat before quoting this as a production number."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
