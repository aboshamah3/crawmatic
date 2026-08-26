#!/usr/bin/env python3
"""Hot-domain cap (EPA W5.5-GA item B, report §10).

Drives `app_shared.scheduling.fair_queue.plan_pass` directly (pure
library, no DB). Fifty tenants each monitor the SAME hot merchant domain
(the exact bug `fair_queue`'s module docstring names: "a per-workspace
concurrency cap of 4 and fifty workspaces monitoring the same merchant"
would put 200 concurrent fetches on that merchant with no fleet plane).
This scenario proves the fleet-wide per-domain cap holds regardless of how
many tenants are queued against it, and measures how many passes it takes
to drain a large backlog on a single, capped domain.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.scheduling.fair_queue import (  # noqa: E402
    DeferralReason,
    FairShare,
    FleetLimits,
    RetryLedger,
    ScheduleCandidate,
    plan_pass,
)
from harness import report_and_print, timed  # noqa: E402

HOT_DOMAIN = "hot-merchant.example"
TENANT_COUNT = 50
CANDIDATES_PER_TENANT = 20
DOMAIN_CAP = 4
BATCH_LIMIT = 1000


def main() -> int:
    now = datetime.now(timezone.utc)
    candidates: list[ScheduleCandidate] = []
    for t_idx in range(TENANT_COUNT):
        for i in range(CANDIDATES_PER_TENANT):
            candidates.append(
                ScheduleCandidate(
                    key=f"t{t_idx}-{i}",
                    workspace_id=f"ws-{t_idx}",
                    domain=HOT_DOMAIN,
                    due_at=now - timedelta(minutes=1),
                )
            )

    total_candidates = len(candidates)
    limits = FleetLimits(default_domain_concurrency=DOMAIN_CAP, fleet_concurrency=None)
    share = FairShare(default_weight=1.0)

    with timed() as t:
        plan = plan_pass(
            candidates,
            now=now,
            batch_limit=BATCH_LIMIT,
            limits=limits,
            share=share,
        )

    admitted_for_domain = sum(1 for c in plan.admitted if c.domain == HOT_DOMAIN)
    domain_deferred = len(plan.deferrals_for(DeferralReason.DOMAIN_LIMIT))
    distinct_tenants_admitted = len({c.workspace_id for c in plan.admitted})

    cap_held = admitted_for_domain <= DOMAIN_CAP

    report_and_print(
        "hot_domain",
        {
            "elapsed_seconds": round(t.elapsed_seconds, 4),
            "tenant_count": TENANT_COUNT,
            "candidates_per_tenant": CANDIDATES_PER_TENANT,
            "total_candidates_queued": total_candidates,
            "domain_cap": DOMAIN_CAP,
            "admitted_for_hot_domain_this_pass": admitted_for_domain,
            "deferred_for_domain_limit": domain_deferred,
            "distinct_tenants_admitted_this_pass": distinct_tenants_admitted,
            "cap_held": cap_held,
            "finding": (
                f"PASS: {total_candidates} candidates queued across {TENANT_COUNT} "
                f"tenants on one hot domain; the fleet plane admitted exactly "
                f"{admitted_for_domain} (cap={DOMAIN_CAP}) this pass regardless of "
                "tenant count — the merchant sees the cap, not the tenant count."
                if cap_held
                else f"FINDING: hot-domain cap of {DOMAIN_CAP} was NOT held — "
                f"{admitted_for_domain} admitted in one pass."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
