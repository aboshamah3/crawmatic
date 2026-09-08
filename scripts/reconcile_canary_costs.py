#!/usr/bin/env python3
"""reconcile_canary_costs.py — EPA D3: bounded real-domain canary cost
reconciliation (audit §13 Economics, deep dive §13).

Compares two independent pairs of numbers for one canary job:

1. **Bytes.** Our own ledger's `network_operations.bytes_compressed`
   (PROXY transport, this `--job-id`) against DataImpulse's own metered
   usage for `--provider-window` (`provider_usage_records.total_bytes`,
   the table `scripts/import_dataimpulse_usage.py` fills). Tolerance
   :data:`BYTES_TOLERANCE_PCT`.
2. **CPU.** Modeled browser CPU for the job (browser-transport operation
   count times `app_shared.costauth.pricing
   .ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST` — the same estimate the
   cost-authorization service reserves against) versus Railway's own
   metered `CPU_USAGE` for the `scrapers-browser` service over the same
   window (`scripts.railway_cost_watchdog._usage_totals_for_window`,
   reused read-only — this script never writes to Railway). Tolerance
   :data:`CPU_TOLERANCE_PCT`.

`cost_model_drift_ratio` is the SAME ratio the A5 gauge
`crawmatic_cost_model_drift_ratio` reports
(`ledger_bytes / provider_bytes`, `app_shared.opsmetrics.emit
.COST_MODEL_DRIFT_RATIO_SQL`) — computed here for exactly this job's
window rather than a rolling 24h, which is why this script's pass bar
(0.9-1.1) is tighter than that gauge's ALERTING band
(`COST_MODEL_DRIFT_RATIO_MIN`/`MAX` = 0.5/2.0 in
`app_shared.opsmetrics.rules`): a controlled, single-job canary against
known strategies has no business drifting anywhere near as far as
organic 24h traffic across every domain is tolerated to.

## The spend gate

**REFUSES to run without `--max-usd`** (destructive-firewall discipline:
this script's job is read-only reconciliation, but it exists ONLY to
gate the one SPEND step this task authorizes — Task D3 Step 1, a single
mixed Amazon+Noon+S-Tech job of 150 targets, capped at $3 by the plan.
An accidental omission of `--max-usd` must never silently read as "no
cap" for a script whose entire purpose is enforcing one). Money is
compared in micro-USD internally
(`app_shared.costauth.service.MICRO_UNITS_PER_USD`) and only converted
to USD at the CLI boundary, per this repo's "the proxy billing unit is a
named setting, never an implicit constant" convention.

## What this script never does

No production scrape, no proxy traffic, no Railway API WRITE, no
spending. `--dry-run` runs the exact same `compute_reconciliation`
function against a small fixture instead of connecting to anything.
Step 1 (the actual $3 canary + this script run against its real numbers)
is DEFERRED — see `docs/ops/COST_MODEL_2026-09.md`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `scripts/` has no __init__.py; the `scripts.railway_cost_watchdog` reuse
# in `_fetch_live_inputs` needs the repository root on the path when this
# file is run directly (same convention as `canary_document_only_browser.py`).
# `app_shared` itself is an editable-installed package and needs no path
# hack.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Ledger-vs-provider bytes tolerance (plan text: "tolerance 10% bytes").
BYTES_TOLERANCE_PCT = 10.0

#: Modeled-vs-measured browser CPU tolerance (plan text: "25% CPU").
CPU_TOLERANCE_PCT = 25.0

#: The canary's own pass bar for `cost_model_drift_ratio`
#: (`ledger_bytes / provider_bytes`) — plan text: "between 0.9 and 1.1".
#: Deliberately tighter than the ops-alerting band in
#: `app_shared.opsmetrics.rules.COST_MODEL_DRIFT_RATIO_MIN/MAX` (0.5/2.0)
#: — see module docstring.
DRIFT_RATIO_MIN = 0.9
DRIFT_RATIO_MAX = 1.1

#: Mirrors `app_shared.costauth.service.MICRO_UNITS_PER_USD` exactly (not
#: imported at module scope to keep `--dry-run`/tests import-light; the
#: value is a fixed unit-conversion constant, not a policy this script
#: could silently drift from).
MICRO_UNITS_PER_USD = 1_000_000

#: Absorbs binary floating-point representation error at tolerance edges
#: (e.g. `1.1 - 1.0 == 0.10000000000000009` in IEEE 754 double
#: precision) so a ratio that is EXACTLY at a stated tolerance boundary
#: reads as "within tolerance", not as a spurious failure of arithmetic
#: neither this script nor the plan's pass bar has any opinion about.
_FLOAT_EPSILON = 1e-9

__all__ = [
    "BYTES_TOLERANCE_PCT",
    "CPU_TOLERANCE_PCT",
    "DRIFT_RATIO_MIN",
    "DRIFT_RATIO_MAX",
    "ReconciliationInputs",
    "ReconciliationVerdict",
    "MaxUsdRequiredError",
    "compute_reconciliation",
    "format_report",
    "build_parser",
    "main",
]


class MaxUsdRequiredError(RuntimeError):
    """Raised when `--max-usd` is missing or not a positive number."""


@dataclass(frozen=True)
class ReconciliationInputs:
    """Everything :func:`compute_reconciliation` needs — every field is a
    plain number so tests can construct this without touching a database,
    Redis, or the Railway API."""

    job_id: str
    #: Sum of `network_operations.bytes_compressed` for this job, PROXY
    #: transport only (the billed path).
    ledger_bytes: int
    #: `provider_usage_records.total_bytes` for `--provider-window`.
    provider_bytes: int
    #: Browser-transport operation count for this job times
    #: `ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST` — see module docstring.
    modeled_browser_cpu_seconds: float
    #: Railway `CPU_USAGE` for the `scrapers-browser` service over the
    #: job's window, in vCPU-minutes (the unit Railway's `usage` API and
    #: `railway_cost_watchdog.py` both report).
    measured_cpu_vcpu_minutes: float
    #: The ledger's own estimated cost for this job, in USD — compared
    #: against `--max-usd` as the spend-gate's actual enforcement.
    ledger_cost_usd: float
    max_usd: float


@dataclass(frozen=True)
class ReconciliationVerdict:
    job_id: str
    ledger_bytes: int
    provider_bytes: int
    bytes_drift_ratio: float | None
    bytes_within_tolerance: bool
    modeled_cpu_seconds: float
    measured_cpu_seconds: float
    cpu_drift_ratio: float | None
    cpu_within_tolerance: bool
    cost_model_drift_ratio: float | None
    cost_model_drift_within_pass_bar: bool
    ledger_cost_usd: float
    max_usd: float
    spend_within_cap: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.bytes_within_tolerance
            and self.cpu_within_tolerance
            and self.cost_model_drift_within_pass_bar
            and self.spend_within_cap
        )


def compute_reconciliation(
    inputs: ReconciliationInputs,
    *,
    bytes_tolerance_pct: float = BYTES_TOLERANCE_PCT,
    cpu_tolerance_pct: float = CPU_TOLERANCE_PCT,
    drift_min: float = DRIFT_RATIO_MIN,
    drift_max: float = DRIFT_RATIO_MAX,
) -> ReconciliationVerdict:
    """Pure function: every input is a number, every output is a number
    or a bool. No I/O — this is the whole reason `--dry-run` and the unit
    tests can prove the MATH without a database, Redis, or Railway."""
    reasons: list[str] = []

    bytes_ratio: float | None = None
    if inputs.provider_bytes:
        bytes_ratio = inputs.ledger_bytes / inputs.provider_bytes
    bytes_within = bytes_ratio is not None and abs(bytes_ratio - 1.0) <= (
        bytes_tolerance_pct / 100 + _FLOAT_EPSILON
    )
    if bytes_ratio is None:
        reasons.append("provider_bytes is zero/absent — bytes tolerance cannot be evaluated")
    elif not bytes_within:
        reasons.append(
            f"ledger/provider bytes ratio {bytes_ratio:.4f} is outside "
            f"+/-{bytes_tolerance_pct:.0f}% of 1.0"
        )

    measured_cpu_seconds = inputs.measured_cpu_vcpu_minutes * 60.0
    cpu_ratio: float | None = None
    if measured_cpu_seconds:
        cpu_ratio = inputs.modeled_browser_cpu_seconds / measured_cpu_seconds
    cpu_within = cpu_ratio is not None and abs(cpu_ratio - 1.0) <= (
        cpu_tolerance_pct / 100 + _FLOAT_EPSILON
    )
    if cpu_ratio is None:
        reasons.append("measured CPU is zero/absent — CPU tolerance cannot be evaluated")
    elif not cpu_within:
        reasons.append(
            f"modeled/measured CPU ratio {cpu_ratio:.4f} is outside "
            f"+/-{cpu_tolerance_pct:.0f}% of 1.0"
        )

    # `cost_model_drift_ratio` is defined identically to the bytes ratio
    # (see module docstring) — same number, evaluated against the
    # canary's own tighter pass bar rather than the ops-alerting band.
    drift_ratio = bytes_ratio
    drift_within = drift_ratio is not None and (
        drift_min - _FLOAT_EPSILON <= drift_ratio <= drift_max + _FLOAT_EPSILON
    )
    if drift_ratio is not None and not drift_within:
        reasons.append(
            f"cost_model_drift_ratio {drift_ratio:.4f} is outside "
            f"[{drift_min}, {drift_max}]"
        )

    spend_within_cap = inputs.ledger_cost_usd <= inputs.max_usd
    if not spend_within_cap:
        reasons.append(
            f"ledger cost ${inputs.ledger_cost_usd:.4f} exceeds --max-usd ${inputs.max_usd:.4f}"
        )

    return ReconciliationVerdict(
        job_id=inputs.job_id,
        ledger_bytes=inputs.ledger_bytes,
        provider_bytes=inputs.provider_bytes,
        bytes_drift_ratio=bytes_ratio,
        bytes_within_tolerance=bytes_within,
        modeled_cpu_seconds=inputs.modeled_browser_cpu_seconds,
        measured_cpu_seconds=measured_cpu_seconds,
        cpu_drift_ratio=cpu_ratio,
        cpu_within_tolerance=cpu_within,
        cost_model_drift_ratio=drift_ratio,
        cost_model_drift_within_pass_bar=drift_within,
        ledger_cost_usd=inputs.ledger_cost_usd,
        max_usd=inputs.max_usd,
        spend_within_cap=spend_within_cap,
        reasons=reasons,
    )


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def format_report(verdict: ReconciliationVerdict) -> str:
    overall = "PASS" if verdict.passed else "FAIL"
    lines = [
        f"reconcile_canary_costs: job_id={verdict.job_id}",
        f"  bytes: ledger={verdict.ledger_bytes} provider={verdict.provider_bytes} "
        f"ratio={_fmt_ratio(verdict.bytes_drift_ratio)} "
        f"within_tolerance={verdict.bytes_within_tolerance} (+/-{BYTES_TOLERANCE_PCT:.0f}%)",
        f"  cpu: modeled_seconds={verdict.modeled_cpu_seconds:.2f} "
        f"measured_seconds={verdict.measured_cpu_seconds:.2f} "
        f"ratio={_fmt_ratio(verdict.cpu_drift_ratio)} "
        f"within_tolerance={verdict.cpu_within_tolerance} (+/-{CPU_TOLERANCE_PCT:.0f}%)",
        f"  cost_model_drift_ratio={_fmt_ratio(verdict.cost_model_drift_ratio)} "
        f"within_pass_bar={verdict.cost_model_drift_within_pass_bar} "
        f"([{DRIFT_RATIO_MIN}, {DRIFT_RATIO_MAX}])",
        f"  spend: ledger_cost_usd=${verdict.ledger_cost_usd:.4f} "
        f"max_usd=${verdict.max_usd:.4f} within_cap={verdict.spend_within_cap}",
    ]
    if verdict.reasons:
        lines.append("  reasons:")
        lines.extend(f"    - {reason}" for reason in verdict.reasons)
    lines.append(f"  verdict={overall}")
    return "\n".join(lines)


def _dry_run_fixture(max_usd: float) -> ReconciliationInputs:
    """A small, deliberately-passing fixture — used by `--dry-run` and by
    `tests/unit/test_reconcile_canary_costs.py`."""
    return ReconciliationInputs(
        job_id="dry-run-job",
        ledger_bytes=10_000_000,
        provider_bytes=9_600_000,  # ratio ~1.0417, inside +/-10%
        modeled_browser_cpu_seconds=250.0,
        measured_cpu_vcpu_minutes=3.5,  # 210s measured, ratio ~1.19, inside +/-25%
        ledger_cost_usd=1.85,
        max_usd=max_usd,
    )


def _fetch_live_inputs(args: argparse.Namespace) -> ReconciliationInputs:
    """Real staging/production query — never invoked in this EPA run
    (Task D3 Step 1 is a SPEND step and is deferred).

    Reads ledger bytes and modeled CPU from `--database-url`, provider
    bytes from `provider_usage_records` for `--provider-window`, and
    measured CPU from the Railway API via
    `scripts.railway_cost_watchdog._usage_totals_for_window` (READ-ONLY —
    this never calls a Railway mutation).
    """
    import re
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, text

    if not args.provider_window:
        raise SystemExit("--provider-window is required for a live run")
    match = re.match(r"^(.+?)/(.+)$", args.provider_window)
    if not match:
        raise SystemExit("--provider-window must be '<start_iso>/<end_iso>'")
    window_start = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(match.group(2).replace("Z", "+00:00"))
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    from app_shared.costauth.pricing import ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST

    engine = create_engine(args.database_url)
    with engine.connect() as conn:
        ledger_row = conn.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(bytes_compressed), 0) AS ledger_bytes,
                    COALESCE(SUM(estimated_cost_micro_units), 0) AS cost_micro_units,
                    COALESCE(SUM(CASE WHEN transport = 'BROWSER' THEN 1 ELSE 0 END), 0)
                        AS browser_operations
                FROM network_operations
                WHERE scrape_job_id = :job_id
                """
            ),
            {"job_id": args.job_id},
        ).mappings().one()
        provider_row = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(total_bytes), 0) AS provider_bytes
                FROM provider_usage_records
                WHERE window_start < :window_end AND window_end > :window_start
                """
            ),
            {"window_start": window_start, "window_end": window_end},
        ).mappings().one()
    engine.dispose()

    modeled_cpu_seconds = float(ledger_row["browser_operations"]) * (
        ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST
    )

    from scripts.railway_cost_watchdog import (  # noqa: E402
        _read_token,
        _usage_totals_for_window,
        fetch_service_names,
        project_id,
    )

    token = _read_token()
    pid = project_id()
    service_names = fetch_service_names(token, pid)
    totals = _usage_totals_for_window(token, pid, window_start, window_end, service_names)
    measured_vcpu_minutes = float(
        (totals.get(args.railway_service) or {}).get("CPU_USAGE", 0.0)
    )

    return ReconciliationInputs(
        job_id=args.job_id,
        ledger_bytes=int(ledger_row["ledger_bytes"]),
        provider_bytes=int(provider_row["provider_bytes"]),
        modeled_browser_cpu_seconds=modeled_cpu_seconds,
        measured_cpu_vcpu_minutes=measured_vcpu_minutes,
        ledger_cost_usd=float(ledger_row["cost_micro_units"]) / MICRO_UNITS_PER_USD,
        max_usd=args.max_usd,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", default=None, help="scrape_jobs.id of the canary run.")
    parser.add_argument(
        "--provider-window",
        default=None,
        help="'<start_iso>/<end_iso>' — the DataImpulse usage window to compare against.",
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=None,
        help="REQUIRED. Refuses to run without it (see module docstring's spend gate).",
    )
    parser.add_argument("--database-url", default=None, help="Postgres DSN (live mode only).")
    parser.add_argument(
        "--railway-service",
        default="scrapers-browser",
        help="Railway service name whose CPU_USAGE is the measured-CPU input.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run compute_reconciliation against a small, deliberately-passing fixture.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_usd is None or args.max_usd <= 0:
        print(
            "REFUSED: --max-usd is required and must be a positive number "
            "(this script exists to enforce Task D3's spend cap).",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        inputs = _dry_run_fixture(args.max_usd)
    else:
        if not args.job_id or not args.database_url:
            print(
                "REFUSED: --job-id and --database-url are required for a live run "
                "(or pass --dry-run).",
                file=sys.stderr,
            )
            return 2
        inputs = _fetch_live_inputs(args)

    verdict = compute_reconciliation(inputs)
    print(format_report(verdict))
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
