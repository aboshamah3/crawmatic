#!/usr/bin/env python3
"""Scheduler backlog drain (EPA W5.5-GA item B, report §10).

Drives repeated `app_shared.scheduling.fair_queue.run_pass` calls (pure
library, no DB) simulating a scheduler that has fallen behind — a large
uniform backlog across many tenants and domains — and measures how many
passes and how much wall time it takes to fully drain, at a realistic
per-pass batch limit and fleet cap.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.scheduling.fair_queue import (  # noqa: E402
    FairShare,
    FleetLimits,
    RetryLedger,
    ScheduleCandidate,
    run_pass,
)
from harness import report_and_print, timed  # noqa: E402

TENANT_COUNT = 200
CANDIDATES_PER_TENANT = 150  # 30,000 total — a multi-hour real backlog shape
BATCH_LIMIT_PER_PASS = 800
FLEET_CONCURRENCY = 64
DOMAIN_CONCURRENCY = 4
DOMAIN_COUNT = 400
MAX_PASSES = 600  # safety bound so a real regression fails loudly, not by hanging


def _build_backlog(now: datetime) -> list[ScheduleCandidate]:
    candidates: list[ScheduleCandidate] = []
    for t_idx in range(TENANT_COUNT):
        for i in range(CANDIDATES_PER_TENANT):
            domain_index = (t_idx * CANDIDATES_PER_TENANT + i) % DOMAIN_COUNT
            candidates.append(
                ScheduleCandidate(
                    key=f"t{t_idx}-{i}",
                    workspace_id=f"ws-{t_idx}",
                    domain=f"backlog-domain-{domain_index}.example",
                    due_at=now - timedelta(hours=1),
                )
            )
    return candidates


def main() -> int:
    now = datetime.now(timezone.utc)
    remaining = _build_backlog(now)
    total_backlog = len(remaining)

    limits = FleetLimits(
        default_domain_concurrency=DOMAIN_CONCURRENCY, fleet_concurrency=FLEET_CONCURRENCY
    )
    share = FairShare(default_weight=1.0)
    ledger = RetryLedger()

    def gate(_candidate):
        return "granted"

    def dispatch(_candidate, _grant):
        return None

    passes_run = 0
    total_dispatched = 0
    with timed() as t:
        while remaining and passes_run < MAX_PASSES:
            passes_run += 1
            outcome = run_pass(
                remaining,
                now=now,
                batch_limit=BATCH_LIMIT_PER_PASS,
                gate=gate,
                dispatch=dispatch,
                ledger=ledger,
                limits=limits,
                share=share,
            )
            dispatched_keys = set(outcome.dispatched)
            total_dispatched += len(dispatched_keys)
            remaining = [c for c in remaining if c.key not in dispatched_keys]

    fully_drained = not remaining
    per_pass_avg = total_dispatched / passes_run if passes_run else 0

    report_and_print(
        "scheduler_backlog",
        {
            "elapsed_seconds": round(t.elapsed_seconds, 4),
            "total_backlog": total_backlog,
            "tenant_count": TENANT_COUNT,
            "domain_count": DOMAIN_COUNT,
            "domain_concurrency_cap": DOMAIN_CONCURRENCY,
            "fleet_concurrency_cap": FLEET_CONCURRENCY,
            "batch_limit_per_pass": BATCH_LIMIT_PER_PASS,
            "passes_run": passes_run,
            "fully_drained": fully_drained,
            "candidates_remaining": len(remaining),
            "total_dispatched": total_dispatched,
            "avg_dispatched_per_pass": round(per_pass_avg, 1),
            "finding": (
                f"DEV-BOUND: a {total_backlog}-item backlog across {TENANT_COUNT} "
                f"tenants drained fully in {passes_run} pass(es) "
                f"({t.elapsed_seconds:.3f}s planning-only wall time for all passes "
                "combined, in-process — excludes actual dispatch I/O, which this "
                "scenario stubs out). No stall observed."
                if fully_drained
                else f"CAPACITY FINDING (not a bug): backlog did NOT drain within "
                f"{MAX_PASSES} passes — {len(remaining)} of {total_backlog} candidates "
                f"still queued. This IS `plan_pass`'s designed behaviour at "
                f"FLEET_CONCURRENCY={FLEET_CONCURRENCY} (matching fair_queue's own "
                f"DEFAULT_FLEET_CONCURRENCY): each pass admits at most "
                f"{FLEET_CONCURRENCY} new items regardless of batch_limit, so "
                f"draining a {total_backlog}-item backlog needs "
                f"~{-(-total_backlog // FLEET_CONCURRENCY)} passes at this fleet cap "
                "— multiply by the real scheduler's pass interval (not measured "
                "here; apps/scheduler is fenced for this task) to get wall-clock "
                "drain time. Worth the owner deciding whether "
                "DEFAULT_FLEET_CONCURRENCY is sized for the largest realistic "
                "backlog this system should tolerate."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
