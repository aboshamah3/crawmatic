#!/usr/bin/env python3
"""Noisy-tenant fairness (EPA W5.5-GA item B, report §10).

Drives `app_shared.scheduling.fair_queue` directly (pure library, no DB —
see that module's own docstring: "Purity... no database, no broker, no
randomness"). One NOISY tenant has thousands of due candidates spread
across many distinct domains (so the fleet/domain caps don't mask fair-
share behaviour); several QUIET tenants have only a handful each. Fairness
means: across repeated passes, the quiet tenants are NOT starved — each of
them gets scheduled within a bounded number of passes, not "eventually,
maybe, after the noisy tenant's queue empties."

This is the algorithm's own contract (`plan_pass`'s docstring: "deficit
weighted round robin over per-workspace queues"), so this scenario is a
demonstration + a numeric measurement, not a search for a bug — the
important output is the actual numbers (passes-to-first-service for the
quietest tenant, share of each pass a `weight=1` quiet tenant receives
against a `weight=1` noisy tenant with 1000x the candidates), timed and
labelled DEV-SERVER-BOUND, exactly as the task requires.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "libs" / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.scheduling.fair_queue import (  # noqa: E402
    FairShare,
    FleetLimits,
    RetryLedger,
    ScheduleCandidate,
    run_pass,
)
from harness import report_and_print, timed  # noqa: E402

NOISY_TENANT_CANDIDATES = 20_000
QUIET_TENANT_COUNT = 8
QUIET_TENANT_CANDIDATES = 3
PASSES_TO_RUN = 25
BATCH_LIMIT_PER_PASS = 500


def _build_candidates(now: datetime) -> tuple[str, list[str], list[ScheduleCandidate]]:
    noisy_id = "ws-noisy"
    quiet_ids = [f"ws-quiet-{i}" for i in range(QUIET_TENANT_COUNT)]
    candidates: list[ScheduleCandidate] = []

    # Noisy tenant: thousands of due items spread over hundreds of
    # distinct domains so the per-domain cap (irrelevant to fairness
    # between TENANTS) never becomes the binding constraint here.
    for i in range(NOISY_TENANT_CANDIDATES):
        candidates.append(
            ScheduleCandidate(
                key=f"noisy-{i}",
                workspace_id=noisy_id,
                domain=f"noisy-domain-{i % 500}.example",
                due_at=now - timedelta(minutes=1),
            )
        )

    for q_idx, ws in enumerate(quiet_ids):
        for i in range(QUIET_TENANT_CANDIDATES):
            candidates.append(
                ScheduleCandidate(
                    key=f"quiet-{q_idx}-{i}",
                    workspace_id=ws,
                    domain=f"quiet-domain-{q_idx}-{i}.example",
                    due_at=now - timedelta(minutes=1),
                )
            )

    return noisy_id, quiet_ids, candidates


def main() -> int:
    now = datetime.now(timezone.utc)
    noisy_id, quiet_ids, all_candidates = _build_candidates(now)

    # Equal weight=1 for every workspace, INCLUDING the noisy one — the
    # scenario is deliberately about the DEFAULT policy (no manual
    # de-prioritization configured), because that's the case that used to
    # starve quiet tenants pre-W4.2.
    share = FairShare(default_weight=1.0)
    limits = FleetLimits(default_domain_concurrency=None, fleet_concurrency=None)
    ledger = RetryLedger()

    remaining = list(all_candidates)
    quiet_first_service_pass: dict[str, int | None] = {ws: None for ws in quiet_ids}
    per_pass_share: list[dict[str, int]] = []

    def gate(_candidate):
        return "granted"

    def dispatch(_candidate, _grant):
        return None

    with timed() as t:
        for pass_number in range(1, PASSES_TO_RUN + 1):
            if not remaining:
                break
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
            counts: dict[str, int] = {}
            for cand in remaining:
                if cand.key in dispatched_keys:
                    counts[cand.workspace_id] = counts.get(cand.workspace_id, 0) + 1
            per_pass_share.append(counts)

            for ws in quiet_ids:
                if quiet_first_service_pass[ws] is None and counts.get(ws, 0) > 0:
                    quiet_first_service_pass[ws] = pass_number

            remaining = [c for c in remaining if c.key not in dispatched_keys]

    quiet_served_total = sum(
        counts.get(ws, 0) for counts in per_pass_share for ws in quiet_ids
    )
    noisy_served_total = sum(counts.get(noisy_id, 0) for counts in per_pass_share)

    report_and_print(
        "noisy_tenant",
        {
            "elapsed_seconds": round(t.elapsed_seconds, 4),
            "passes_run": len(per_pass_share),
            "noisy_tenant_candidates": NOISY_TENANT_CANDIDATES,
            "quiet_tenant_count": QUIET_TENANT_COUNT,
            "quiet_tenant_candidates_each": QUIET_TENANT_CANDIDATES,
            "batch_limit_per_pass": BATCH_LIMIT_PER_PASS,
            "quiet_first_service_pass": quiet_first_service_pass,
            "all_quiet_tenants_served_by_pass_1": all(
                v == 1 for v in quiet_first_service_pass.values()
            ),
            "quiet_served_total": quiet_served_total,
            "noisy_served_total": noisy_served_total,
            "candidates_remaining_unserved": len(remaining),
            "finding": (
                "PASS: every quiet tenant received service in pass 1 despite the "
                f"noisy tenant queuing {NOISY_TENANT_CANDIDATES / QUIET_TENANT_CANDIDATES:.0f}x "
                "more candidates than a single quiet tenant, confirming "
                "deficit-weighted-round-robin fairness under default (all weight=1) "
                "policy."
                if all(v == 1 for v in quiet_first_service_pass.values())
                else "FINDING: at least one quiet tenant was NOT served in pass 1 "
                "— starvation under default policy, needs investigation."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
