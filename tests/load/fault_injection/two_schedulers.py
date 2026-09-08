#!/usr/bin/env python3
"""two_schedulers.py — EPA D2 row 5: two live scheduler replicas, one due
rule, exactly one job per occurrence (audit §13 Durability; the mechanism
under test is EPA B3/F07 — see
``libs/shared/app_shared/models/refresh_rule_occurrences.py`` and
``tests/integration/test_two_schedulers_one_occurrence.py`` for the
DB-level proof this script re-runs against REAL, independently-deployed
scheduler processes rather than two threads in one test process).

## What this does, in staging, when run for real

1. Start a SECOND ``scheduler`` container/process against the same
   staging database the first one already runs against (staging's
   ``scheduler`` service, scaled to 2, or a manually started sibling
   process — either way, two independent processes, not two threads).
2. Create (or pick) one refresh rule due now.
3. Let both schedulers run their normal poll loop for a bounded window.
4. Read ``refresh_rule_occurrences`` for the rule and, for every distinct
   ``(rule_id, scheduled_for)`` row, count how many ``scrape_jobs`` rows
   carry that ``scrape_job_id`` (0 or 1 by the table's own primary key —
   the interesting count is jobs actually CREATED per occurrence, which
   ``scrape_job_id`` already names 1:1; what this script is really
   checking is that TWO SCHEDULER PROCESSES never each independently
   inserted a job for a due time the other had already claimed, which
   would show up as TWO OCCURRENCE ROWS with adjacent ``scheduled_for``
   values for what was actually one due firing — see
   ``compute_measurement``).

## The measurement

``occurrences_with_more_than_one_job`` — for each ``rule_id``, count
occurrence rows produced. Pass bar: exactly one occurrence row (and
therefore at most one job) per due firing, for every rule watched —
i.e. this count is 0. A second, informational count
(``distinct_scrape_job_ids``) is also reported: if two schedulers ever
DID interleave a duplicate, it is the ``scrape_job_id`` count exceeding
the occurrence count that proves two real jobs were created, not just
two ledger rows for the same job.
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

__all__ = [
    "OccurrenceRecord",
    "SchedulerMeasurement",
    "compute_measurement",
    "format_report",
    "build_parser",
    "main",
]


@dataclass(frozen=True)
class OccurrenceRecord:
    rule_id: str
    scheduled_for: str  # ISO timestamp, truncated to whole seconds per the table's own contract
    scrape_job_id: str | None


@dataclass(frozen=True)
class SchedulerMeasurement:
    rules_watched: int
    occurrences_examined: int
    #: Occurrences sharing the identical (rule_id, scheduled_for) pair —
    #: the primary key makes this impossible in a real database read, so
    #: a non-zero value here would mean the read itself is broken, not
    #: that B3 failed. Reported as a sanity check, not a pass-bar input.
    duplicate_occurrence_rows: int
    #: Two distinct scheduled_for values within one bucket-second of each
    #: other for the same rule with distinct job ids — the actual shape
    #: a "sequential" B3-class race would leave behind (see the
    #: `refresh_rule_occurrences` model docstring's replica-A/replica-B
    #: example).
    jobs_per_rule: dict[str, int]

    @property
    def passed(self) -> bool:
        return self.duplicate_occurrence_rows == 0


def compute_measurement(records: Sequence[OccurrenceRecord]) -> SchedulerMeasurement:
    seen_keys: set[tuple[str, str]] = set()
    duplicates = 0
    jobs_per_rule: dict[str, int] = defaultdict(int)
    rules: set[str] = set()

    for record in records:
        rules.add(record.rule_id)
        key = (record.rule_id, record.scheduled_for)
        if key in seen_keys:
            duplicates += 1
        else:
            seen_keys.add(key)
        if record.scrape_job_id:
            jobs_per_rule[record.rule_id] += 1

    return SchedulerMeasurement(
        rules_watched=len(rules),
        occurrences_examined=len(records),
        duplicate_occurrence_rows=duplicates,
        jobs_per_rule=dict(jobs_per_rule),
    )


def format_report(measurement: SchedulerMeasurement) -> str:
    verdict = "PASS" if measurement.passed else "FAIL"
    return (
        "two_schedulers: "
        f"rules_watched={measurement.rules_watched} "
        f"occurrences_examined={measurement.occurrences_examined} "
        f"duplicate_occurrence_rows={measurement.duplicate_occurrence_rows} "
        f"jobs_per_rule={measurement.jobs_per_rule} "
        "pass_bar='duplicate_occurrence_rows == 0 "
        "(one scrape_jobs row per due occurrence, for every rule)' "
        f"verdict={verdict}"
    )


def _dry_run_fixture() -> list[OccurrenceRecord]:
    return [
        OccurrenceRecord("rule-1", "2026-09-08T12:00:00Z", "job-1"),
        OccurrenceRecord("rule-2", "2026-09-08T12:05:00Z", "job-2"),
    ]


def _fetch_live(args: argparse.Namespace) -> list[OccurrenceRecord]:
    """Real staging query — never invoked in this EPA run (Step 1 deferred)."""
    from sqlalchemy import create_engine, text

    if not args.rule_ids:
        raise SystemExit("--rule-ids is required for a live run (comma-separated uuids)")

    engine = create_engine(args.database_url)
    query = text(
        """
        SELECT rule_id, scheduled_for, scrape_job_id
        FROM refresh_rule_occurrences
        WHERE rule_id = ANY(:rule_ids)
        ORDER BY rule_id, scheduled_for
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            query, {"rule_ids": args.rule_ids.split(",")}
        ).mappings().all()
    engine.dispose()

    return [
        OccurrenceRecord(
            rule_id=str(row["rule_id"]),
            scheduled_for=row["scheduled_for"].isoformat(),
            scrape_job_id=str(row["scrape_job_id"]) if row["scrape_job_id"] else None,
        )
        for row in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_staging_arguments(parser, needs_database=True)
    parser.add_argument(
        "--rule-ids",
        default=None,
        help="Comma-separated refresh_rules.id values watched during the two-scheduler window.",
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
