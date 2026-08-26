#!/usr/bin/env python3
"""N+1 detection on the daily-rollup read path (EPA W5.5-GA item B, report
§10).

Needs the scratch Postgres from `run_load_suite.sh`
(`LOAD_TEST_SYSTEM_DATABASE_URL` — the BYPASSRLS role, matching how the
real `MAINTENANCE_DAILY_ROLLUP` Celery task actually calls
`run_daily_rollup` via `app_shared.database.get_system_session`; see
`tests/integration/test_daily_rollup_live.py`'s header comment).

Target: `app_shared.maintenance.rollups.run_daily_rollup` — NOT a fenced
file (only `app_shared.netledger.rollups` is fenced for this task; this is
`app_shared.maintenance.rollups`, a different module). Called with
`dry_run=True` so this scenario only ever reads.

Method: seed N distinct `(workspace_id, product_variant_id)` pairs, each
with a `variant_price_states` row and a handful of `price_observations`
rows for the target day, then run `run_daily_rollup` with a
`harness.QueryCounter` attached to the engine and record the total SQL
statement count at N = 10, 50, 200. `run_daily_rollup`'s own docstring
states the shape plainly: one cross-tenant driver scan, then "every
subsequent read/write... carries an explicit workspace_id=" — i.e. per-
pair queries. If the statement count grows roughly as `1 + k*N` for some
constant k (rather than staying ~constant), that is the N+1 signature,
made empirical rather than theoretical.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app_shared.enums import AlertSeverity, AlertType, WorkspaceStatus  # noqa: E402
from app_shared.maintenance.rollups import run_daily_rollup  # noqa: E402
from app_shared.models.alerts import VariantPriceState  # noqa: E402
from app_shared.models.identity import Workspace  # noqa: E402
from app_shared.models.observations import PriceObservation  # noqa: E402
from harness import report_and_print, scratch_database_urls  # noqa: E402

PAIR_COUNTS = (10, 50, 200)
OBSERVATIONS_PER_PAIR = 3
TARGET_DATE = (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _seed_pairs(session: Session, workspace_id: uuid.UUID, n: int) -> None:
    day_mid = datetime.combine(TARGET_DATE, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=12
    )
    for i in range(n):
        product_id = uuid.uuid4()
        variant_id = uuid.uuid4()
        session.add(
            VariantPriceState(
                workspace_id=workspace_id,
                product_id=product_id,
                product_variant_id=variant_id,
                client_price=Decimal("100.00"),
                currency="USD",
                cheapest_competitor_price=None,
                average_competitor_price=None,
                highest_competitor_price=None,
                comparable_competitor_count=0,
                latest_alert_type=AlertType.NO_COMPETITOR_DATA,
                latest_alert_severity=AlertSeverity.NONE,
                latest_alert_state_id=None,
                calculated_at=day_mid,
            )
        )
        for j in range(OBSERVATIONS_PER_PAIR):
            session.add(
                PriceObservation(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    scraped_at=day_mid + timedelta(minutes=j),
                    match_id=uuid.uuid4(),
                    product_id=product_id,
                    product_variant_id=variant_id,
                    scrape_job_id=None,
                    price=Decimal("95.00") + j,
                    currency="USD",
                    success=True,
                    comparable=True,
                )
            )
    session.commit()


def main() -> int:
    urls = scratch_database_urls()
    engine = create_engine(urls["system"])

    from harness import QueryCounter

    results = []
    for n in PAIR_COUNTS:
        workspace_id = uuid.uuid4()
        with Session(engine) as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    name=f"n1-load-{n}",
                    slug=f"n1-load-{n}-{workspace_id.hex[:8]}",
                    status=WorkspaceStatus.ACTIVE,
                )
            )
            session.commit()
            _seed_pairs(session, workspace_id, n)

            counter = QueryCounter()
            counter.attach(engine)
            try:
                report = run_daily_rollup(session, target_date=TARGET_DATE, dry_run=True)
            finally:
                counter.detach()

            results.append(
                {
                    "pairs_seeded": n,
                    "rollups_would_upsert": report.rollups_upserted,
                    "sql_statements_executed": counter.count,
                }
            )

    # Linear-regression-free, deliberately simple slope check: statements
    # per pair between the smallest and largest run. A flat read path
    # (e.g. one batched query regardless of N) would show this ratio
    # collapsing toward 0 as N grows; an N+1 path shows it converging to
    # a POSITIVE constant (here: 2 reads/pair — client-state + day-
    # observations, matching the source exactly).
    smallest, largest = results[0], results[-1]
    delta_statements = largest["sql_statements_executed"] - smallest["sql_statements_executed"]
    delta_pairs = largest["pairs_seeded"] - smallest["pairs_seeded"]
    statements_per_additional_pair = delta_statements / delta_pairs if delta_pairs else 0

    is_n_plus_one = statements_per_additional_pair >= 1.5  # ~2 expected; comfortably > "flat"

    report_and_print(
        "n1_detection",
        {
            "target": "app_shared.maintenance.rollups.run_daily_rollup",
            "observations_per_pair": OBSERVATIONS_PER_PAIR,
            "results": results,
            "statements_per_additional_pair": round(statements_per_additional_pair, 2),
            "n_plus_one_confirmed": is_n_plus_one,
            "finding": (
                "N+1 CONFIRMED: app_shared.maintenance.rollups.run_daily_rollup "
                f"issues ~{statements_per_additional_pair:.1f} additional SQL "
                "statements per additional (workspace, variant) pair beyond the "
                "single cross-tenant driver scan — matches the source exactly "
                "(_client_state_stmt + _day_observations_stmt per pair, both "
                "documented in the module's own docstrings as executing "
                "'once per (workspace, variant) pair'). Query count scales "
                "LINEARLY with row count. NOT a fenced file "
                "(app_shared.maintenance.rollups, distinct from the fenced "
                "app_shared.netledger.rollups) — reported here as a finding "
                "for the owner, not fixed by this task per its scope (load "
                "tests + findings, not remediation)."
                if is_n_plus_one
                else "No N+1 signature detected in this run — statement count "
                "did not scale linearly with pair count."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
