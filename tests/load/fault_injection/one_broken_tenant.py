#!/usr/bin/env python3
"""one_broken_tenant.py — EPA D2 row 6: one tenant whose fixture store
blocks 100% of requests must not stall anyone else (audit §13 Tenant
fairness; the workspace-scoped rate/concurrency machinery under test is
``app_shared.limiter`` keyed by ``workspace_id`` — see
``libs/shared/app_shared/limiter/keys.py`` — and the cost-authorization
concurrency cap in ``libs/shared/app_shared/costauth/service.py``'s
``_check_concurrency``).

## What this does, in staging, when run for real

1. Point one workspace's target set at a fixture origin (or a real
   staging domain override) configured to answer every request with a
   block signal (HTTP 403/429, or a TCP RST — whatever the fixture
   origin/``domain_playbooks`` override supports) for the whole run —
   "100% blocks", not a flaky domain.
2. Give several OTHER workspaces ordinary due work on their own,
   unrelated domains at the same time.
3. Let the scheduler/dispatcher run for a bounded window.
4. Read back, per workspace: targets attempted, targets that ended
   blocked/failed, targets that completed normally.

## The measurement

* ``broken_tenant_block_rate`` — blocked-or-failed / attempted for the
  broken workspace. Pass bar: ``== 1.0`` (the fixture really did block
  everything; a script that "proves fairness" against a domain that
  quietly let some requests through would be proving nothing).
* ``other_tenants_progress`` — total targets COMPLETED, summed across
  every other workspace, during the same window. Pass bar: ``> 0`` — due
  work for unrelated tenants must start and finish while the broken
  tenant is failing, not queue up behind it.
* ``other_tenants_collateral_block_rate`` — blocked-or-failed / attempted
  across every OTHER workspace. Pass bar: this must not be pulled toward
  1.0 by the broken tenant; reported against a
  ``--baseline-block-rate`` the operator supplies from an unaffected
  run (default 0.05 — a generous baseline failure rate for ordinary
  scraping), and passes when it does not exceed that baseline by more
  than :data:`COLLATERAL_TOLERANCE`.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
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

#: How far above the operator-supplied baseline other tenants'
#: collateral block rate is allowed to drift before this fails.
COLLATERAL_TOLERANCE = 0.10

__all__ = [
    "COLLATERAL_TOLERANCE",
    "TargetOutcome",
    "FairnessMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class TargetOutcome:
    workspace_id: str
    is_broken_tenant: bool
    status: str  # "COMPLETED" / "FAILED" / "BLOCKED" / "SKIPPED" / "PENDING"


@dataclass(frozen=True)
class FairnessMeasurement:
    broken_tenant_attempted: int
    broken_tenant_blocked_or_failed: int
    other_tenants_attempted: int
    other_tenants_completed: int
    other_tenants_blocked_or_failed: int
    baseline_block_rate: float

    @property
    def broken_tenant_block_rate(self) -> float | None:
        if self.broken_tenant_attempted == 0:
            return None
        return self.broken_tenant_blocked_or_failed / self.broken_tenant_attempted

    @property
    def other_tenants_collateral_block_rate(self) -> float | None:
        if self.other_tenants_attempted == 0:
            return None
        return self.other_tenants_blocked_or_failed / self.other_tenants_attempted

    @property
    def passed(self) -> bool:
        block_rate = self.broken_tenant_block_rate
        collateral = self.other_tenants_collateral_block_rate
        return (
            block_rate is not None
            and block_rate == 1.0
            and self.other_tenants_completed > 0
            and collateral is not None
            and collateral <= self.baseline_block_rate + COLLATERAL_TOLERANCE
        )


_TERMINAL_BAD = {"FAILED", "BLOCKED"}


def compute_measurement(
    outcomes: Sequence[TargetOutcome], *, baseline_block_rate: float
) -> FairnessMeasurement:
    broken_attempted = 0
    broken_bad = 0
    other_attempted = 0
    other_completed = 0
    other_bad = 0

    for outcome in outcomes:
        if outcome.status == "PENDING":
            continue  # never dispatched — not an "attempt"
        if outcome.is_broken_tenant:
            broken_attempted += 1
            if outcome.status in _TERMINAL_BAD:
                broken_bad += 1
        else:
            other_attempted += 1
            if outcome.status == "COMPLETED":
                other_completed += 1
            elif outcome.status in _TERMINAL_BAD:
                other_bad += 1

    return FairnessMeasurement(
        broken_tenant_attempted=broken_attempted,
        broken_tenant_blocked_or_failed=broken_bad,
        other_tenants_attempted=other_attempted,
        other_tenants_completed=other_completed,
        other_tenants_blocked_or_failed=other_bad,
        baseline_block_rate=baseline_block_rate,
    )


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.3f}"


def format_report(measurement: FairnessMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "one_broken_tenant: "
        f"broken_tenant_attempted={measurement.broken_tenant_attempted} "
        f"broken_tenant_block_rate={_fmt_rate(measurement.broken_tenant_block_rate)} "
        f"other_tenants_attempted={measurement.other_tenants_attempted} "
        f"other_tenants_completed={measurement.other_tenants_completed} "
        f"other_tenants_collateral_block_rate="
        f"{_fmt_rate(measurement.other_tenants_collateral_block_rate)} "
        f"baseline_block_rate={measurement.baseline_block_rate:.3f} "
        "pass_bar='broken_tenant_block_rate == 1.0 and other_tenants_completed > 0 "
        "and other_tenants_collateral_block_rate <= baseline + "
        f"{COLLATERAL_TOLERANCE}' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> list[TargetOutcome]:
    outcomes = [TargetOutcome("ws-broken", True, "BLOCKED") for _ in range(20)]
    for i in range(30):
        outcomes.append(
            TargetOutcome(f"ws-ok-{i % 5}", False, "COMPLETED" if i % 10 else "FAILED")
        )
    return outcomes


def _fetch_live(args: argparse.Namespace) -> list[TargetOutcome]:
    """Real staging query — never invoked in this EPA run (Step 1 deferred)."""
    from sqlalchemy import create_engine, text

    if not args.broken_workspace_id or not args.window_start or not args.window_end:
        raise SystemExit(
            "--broken-workspace-id, --window-start and --window-end are required for a live run"
        )

    engine = create_engine(args.database_url)
    query = text(
        """
        SELECT workspace_id, status
        FROM scrape_job_targets
        WHERE created_at >= :window_start AND created_at < :window_end
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            query,
            {"window_start": args.window_start, "window_end": args.window_end},
        ).mappings().all()
    engine.dispose()

    return [
        TargetOutcome(
            workspace_id=str(row["workspace_id"]),
            is_broken_tenant=str(row["workspace_id"]) == args.broken_workspace_id,
            status=row["status"],
        )
        for row in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True)
    parser.add_argument("--broken-workspace-id", default=None)
    parser.add_argument("--window-start", default=None, help="ISO 8601 UTC timestamp")
    parser.add_argument("--window-end", default=None, help="ISO 8601 UTC timestamp")
    parser.add_argument(
        "--baseline-block-rate",
        type=float,
        default=0.05,
        help="Ordinary (unaffected) block/fail rate to compare other tenants against.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_database=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    outcomes = _dry_run_fixture() if args.dry_run else _fetch_live(args)
    measurement = compute_measurement(outcomes, baseline_block_rate=args.baseline_block_rate)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
