#!/usr/bin/env python3
"""seed_workspace_entitlements.py — seed the C3 entitlement gate (EPA go-live).

`CostAuthorizationService._check_entitlement`
(`libs/shared/app_shared/costauth/service.py`) denies ALL paid work for a
workspace with no `workspace_entitlements` row, a non-`ACTIVE` state, or
evidence older than `DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS`
(86400). That is the correct fail-closed posture — and it means an engine
whose `workspace_entitlements` table is empty scrapes nothing at all. The
table is empty in production and has no writer: W1.1 owns the real
SaaS->engine billing replication and has not landed.

Owner decision (2026-08-26): **seed now, real ingest later.** This script
is the "seed now" half. Run it once at deploy (and again after adding a
workspace); the scheduler's `entitlement_refresh` cadence
(`CADENCE_ENTITLEMENT_REFRESH`) then keeps the seeded rows from ever
aging into a stale-deny.

What it will and will not touch
--------------------------------
Every workspace falls into exactly one of three buckets, and the third is
the one this script exists to protect (see
`app_shared.costauth.entitlements` for the full contract):

* no entitlement row -> one is created, `ACTIVE`, observed now;
* a row whose `evidence_version` starts with `seeded-` -> re-stamped;
* **anything else -> left exactly as found.** A row with any other
  version tag was written by the future real ingest (or by an operator),
  and real billing evidence always beats a placeholder. The script
  reports those workspaces rather than silently skipping them, because a
  growing "left alone" count across runs is how the real ingest landing
  becomes visible to whoever is still running this.

Session seam
------------
Runs on the sanctioned BYPASSRLS system session
(`app_shared.database.get_system_session` -> `SYSTEM_DATABASE_URL`,
falling back to `AUTH_DATABASE_URL`). `workspace_entitlements` is
workspace-owned and `FORCE ROW LEVEL SECURITY` is on, so a fleet-wide
pass is structurally cross-tenant — the same seam, for the same reason,
as the C3 lease sweeper and `scripts/backfill_daily_rollups.py`.

Dry-run is the default
----------------------
Without `--apply` the script classifies every workspace, prints exactly
what it WOULD write, and persists nothing — and the guarantee is
DATABASE-level, not merely app-level: the session's first statement is
`SET TRANSACTION READ ONLY` (the construct that governs the CURRENT
transaction, per `scripts/backfill_daily_rollups.py`'s hard-won note),
and the seeding pass is additionally called with `apply=False` so no
write is ever built. `--apply` drops the guard and commits once, at the
end: the pass is a handful of rows on a table with one row per
workspace, so there is nothing to gain from incremental commits and a
single transaction means a failure leaves the gate exactly as it was.

Importable without connecting to anything: the engine is constructed
inside `main`, never at import time (the `scripts/seed_bootstrap.py`
convention), so `tests/unit/test_seed_workspace_entitlements.py` collects
with zero environment variables set.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.costauth.entitlements import (
    EntitlementSeedReport,
    seed_workspace_entitlements,
    seeded_evidence_version,
)

SessionFactory = Callable[[], Session]


def run_seed(
    *,
    now: datetime,
    evidence_version: str,
    apply: bool,
    session_factory: SessionFactory,
) -> EntitlementSeedReport:
    """Classify (and, with ``apply``, write) every workspace's entitlement row.

    One session, one transaction — see the module docstring for why the
    whole fleet goes in a single commit rather than per workspace.

    ``apply=False`` issues ``SET TRANSACTION READ ONLY`` as the session's
    very FIRST statement (it only governs the current transaction if
    nothing has established one yet) and then runs the pass with
    ``apply=False``, which never builds a write at all. The two guards
    are independent on purpose: the read-only transaction would reject a
    write, and the pass never attempts one, so neither is load-bearing
    alone. ``session.rollback()`` closes the dry run out for hygiene.
    """
    session = session_factory()
    try:
        if not apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        report = seed_workspace_entitlements(
            session, now=now, evidence_version=evidence_version, apply=apply
        )
        if apply:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()
    return report


def format_report(report: EntitlementSeedReport, *, apply: bool) -> str:
    """One human-readable summary line plus the left-alone detail.

    The left-alone workspace ids are printed in full (not merely counted)
    because they are the rows an operator must be able to go look at: a
    workspace this script refuses to touch is either the real ingest
    working, or an entitlement someone hand-edited, and those need very
    different follow-ups.
    """
    mode = "APPLY" if apply else "DRY-RUN"
    lines = [
        f"seed_workspace_entitlements mode={mode} "
        f"workspaces_seen={report.workspaces_seen} "
        f"created={len(report.created)} refreshed={len(report.refreshed)} "
        f"left_alone={len(report.left_alone)}"
    ]
    for workspace_id in report.left_alone:
        lines.append(
            f"  left alone workspace_id={workspace_id} "
            "(evidence_version was not written by this seeder — a real "
            "ingest or an operator owns this row)"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently seed workspace_entitlements so the C3 cost-"
            "authorization gate stops denying every workspace. Never "
            "overwrites a row a real billing ingest wrote."
        )
    )
    parser.add_argument(
        "--evidence-version",
        default=None,
        help=(
            "Version tag to stamp (default: seeded-<today UTC>). Must start "
            "with 'seeded-' — the scheduler's entitlement_refresh cadence "
            "recognises seeded rows by exactly that prefix, and a row it "
            "cannot recognise goes stale-deny within 24h."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Commit the seed. Without this flag, runs a dry-run: reports "
            "what WOULD be created/refreshed/left alone, then rolls back — "
            "no row is persisted."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: resolve the version tag, connect, seed, report.

    The database connection (`app_shared.database.get_system_sessionmaker`)
    is imported and constructed HERE, never at module import time, so a
    bare `import scripts.seed_workspace_entitlements` does no I/O and
    needs no environment (`scripts/seed_bootstrap.py` /
    `scripts/backfill_daily_rollups.py` convention).
    """
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    evidence_version = args.evidence_version or seeded_evidence_version(
        date_type.today()
    )

    from app_shared.database import get_system_sessionmaker

    try:
        report = run_seed(
            now=now,
            evidence_version=evidence_version,
            apply=args.apply,
            session_factory=get_system_sessionmaker(),
        )
    except ValueError as exc:
        print(f"seed_workspace_entitlements: {exc}", file=sys.stderr)
        return 1

    print(f"seed_workspace_entitlements evidence_version={evidence_version}")
    print(format_report(report, apply=args.apply))
    if not args.apply:
        print("seed_workspace_entitlements: nothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
