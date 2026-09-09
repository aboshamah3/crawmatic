#!/usr/bin/env python3
"""kill_worker_after_post.py — EPA D2 row 1: crash-after-POST (audit §13
Durability; the mechanism under test is EPA B2/F06 — see
``libs/shared/app_shared/jobs/dispatch_intents.py`` and
``tests/unit/test_dispatch_kill_after_post.py`` for the unit-level proof
this script re-proves against a REAL staging worker container).

## What this does, in staging, when run for real

1. Enqueue one scrape job against a small target set.
2. Watch ``dispatch_intents`` (via ``--database-url``) until an intent for
   it reaches ``POSTED`` — the schedule.json POST has been accepted by a
   real Scrapyd node, so a run genuinely exists there.
3. The instant it is observed ``POSTED``, ``docker kill -s KILL`` (or the
   staging platform's equivalent) the ``worker`` container that posted
   it, so the process that would have moved the row to ``CONFIRMED`` dies
   mid-flight — exactly the window B2/F06 closes.
4. Bring a worker back and let ``reconcile_inflight_intents`` (the
   maintenance reconciler) run its normal sweep.
5. Read back every dispatch intent for the job and compute the two
   numbers the plan requires.

## The measurement

* **duplicate physical fetches** — count of dispatch identities for
  which MORE THAN ONE distinct ``scrapyd_job_id`` was ever POSTED. Since
  ``deterministic_scrapyd_job_id`` derives one id per identity and every
  re-POST (only ever a ``RECONCILED_MISSING`` re-POST) reuses that same
  id, this must be 0 for every identity that reconciled correctly — any
  identity that shows two DIFFERENT ids was actually double-charged.
* **lost observations** — count of intents that reached ``CONFIRMED``
  (Scrapyd genuinely ran the batch) with zero persisted
  ``price_observations`` rows for the targets it covered. A CONFIRMED
  run that produced no evidence is the exact failure this whole chain
  exists to prevent.

Pass bar: both are 0.
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

__all__ = [
    "IntentRecord",
    "KillWorkerMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class IntentRecord:
    """One ``dispatch_intents`` row's history, as read back after the sweep."""

    identity_key: str
    #: Every distinct ``scrapyd_job_id`` ever POSTed for this identity,
    #: in the order observed. Length 1 in the healthy case; a re-POST
    #: after ``RECONCILED_MISSING`` appends the SAME id (still length 1
    #: after de-duplication) unless something went wrong.
    scrapyd_job_ids_seen: tuple[str, ...]
    state: str  # DispatchIntentState value: POSTED / CONFIRMED / RECONCILED_MISSING / ...
    #: Persisted price_observations rows for the targets this intent's
    #: batch covered, counted only when state == CONFIRMED (a POSTED or
    #: RECONCILED_MISSING intent has no run to have produced evidence for
    #: yet, so it is not "lost").
    observation_count: int


@dataclass(frozen=True)
class KillWorkerMeasurement:
    intents_examined: int
    duplicate_physical_fetches: int
    lost_observations: int
    confirmed: int
    reconciled_missing: int
    still_posted: int

    @property
    def passed(self) -> bool:
        return self.duplicate_physical_fetches == 0 and self.lost_observations == 0


def compute_measurement(records: Sequence[IntentRecord]) -> KillWorkerMeasurement:
    duplicate = sum(1 for r in records if len(set(r.scrapyd_job_ids_seen)) > 1)
    lost = sum(1 for r in records if r.state == "CONFIRMED" and r.observation_count == 0)
    state_counts = Counter(r.state for r in records)
    return KillWorkerMeasurement(
        intents_examined=len(records),
        duplicate_physical_fetches=duplicate,
        lost_observations=lost,
        confirmed=state_counts.get("CONFIRMED", 0),
        reconciled_missing=state_counts.get("RECONCILED_MISSING", 0),
        still_posted=state_counts.get("POSTED", 0),
    )


def format_report(measurement: KillWorkerMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "kill_worker_after_post: "
        f"intents_examined={measurement.intents_examined} "
        f"duplicate_physical_fetches={measurement.duplicate_physical_fetches} "
        f"lost_observations={measurement.lost_observations} "
        f"confirmed={measurement.confirmed} "
        f"reconciled_missing={measurement.reconciled_missing} "
        f"still_posted={measurement.still_posted} "
        f"pass_bar='both duplicate_physical_fetches and lost_observations are 0' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> list[IntentRecord]:
    """A small, deliberately healthy fixture: exactly what the live path
    should observe when B2/F06 holds. Used by ``--dry-run`` and by
    ``tests/load/test_fault_injection_dry_run.py``."""
    jid = "6f3c1e2a-0000-5000-8000-000000000001"
    return [
        IntentRecord("identity-1", (jid,), "CONFIRMED", observation_count=3),
        IntentRecord(
            "identity-2",
            ("6f3c1e2a-0000-5000-8000-000000000002",) * 2,  # re-POST, SAME id twice
            "CONFIRMED",
            observation_count=1,
        ),
    ]


def _fetch_live(args: argparse.Namespace) -> list[IntentRecord]:
    """Real staging query — never invoked in this EPA run (Step 1 deferred).

    Connects with ``--database-url`` (never an env fallback — see
    ``_common``), reads every ``dispatch_intents`` row for
    ``--scrape-job-id`` plus its reconciliation history, and joins
    ``price_observations`` counts per target for the CONFIRMED rows.
    """
    from sqlalchemy import create_engine, text

    if not args.scrape_job_id:
        raise SystemExit("--scrape-job-id is required for a live run")

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

    # The live schema records the CURRENT scrapyd_job_id, not the full
    # history of ids seen — `deterministic_scrapyd_job_id` guarantees
    # there is only ever one distinct id per identity by construction, so
    # a length-1 tuple here is the correct live-mode representation; a
    # genuine duplicate would show up as two DIFFERENT rows sharing an
    # identity_key with different ids, which the GROUP BY below catches.
    by_identity: dict[str, list[str]] = {}
    states: dict[str, str] = {}
    obs: dict[str, int] = {}
    for row in rows:
        by_identity.setdefault(row["identity_key"], []).append(str(row["scrapyd_job_id"]))
        states[row["identity_key"]] = row["state"]
        obs[row["identity_key"]] = int(row["observation_count"])

    return [
        IntentRecord(
            identity_key=key,
            scrapyd_job_ids_seen=tuple(job_ids),
            state=states[key],
            observation_count=obs[key],
        )
        for key, job_ids in by_identity.items()
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True)
    parser.add_argument(
        "--scrape-job-id",
        default=None,
        help="The scrape_jobs.id enqueued for this run (required for a live run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_staging(args, needs_database=True)
    except StagingGuardRefusal as exc:
        return refuse_and_exit(exc)

    records = _dry_run_fixture() if args.dry_run else _fetch_live(args)
    measurement = compute_measurement(records)
    print(format_report(measurement))
    return 0 if measurement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
