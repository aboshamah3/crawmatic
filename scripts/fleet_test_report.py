#!/usr/bin/env python3
"""fleet_test_report.py — the audit §13 release-gate table, with pass/fail (EPA D1, F22).

Reads, for one run window:

* the **D5 daily scorecard** (``fleet_daily_scorecard``, via
  ``app_shared.maintenance.scorecard.read_scorecard_range``), and
* the **A5 baseline gauges** (``app_shared.opsmetrics.emit
  .collect_baseline_metrics``), and
* a **measurements file** the operator fills in from the step-3 fleet
  run — the figures nothing in the engine records for itself (rollup
  wall clock, DB pool wait p95, ``alembic upgrade head`` duration,
  retention dry-run duration, backup export bytes/seconds, the
  fault-injection matrix verdicts from D2),

and prints the twelve-row release-gate table from
``CORE_PRODUCTION_AUDIT_2026-09-07.md`` §13 with a verdict per row.

THE ONE RULE THIS SCRIPT IS BUILT ON
------------------------------------
**A gate with no measurement is ``NO DATA``, never ``PASS``.** It is a
release gate; the failure mode that matters is not "we said fail when it
passed", it is "we said pass because the number was missing". So an
absent input never defaults, is never coerced to zero, and never
borrows a neighbouring row's evidence — exactly the discipline
``app_shared.maintenance.scorecard`` applies to the scorecard's own
columns, and for the same reason. The exit status makes the difference
visible to CI:

* ``0`` — every row PASS or MANUAL (the two rows §13 settles with
  labelled evidence rather than a number)
* ``1`` — at least one row FAIL
* ``2`` — CLI refusal (bad arguments)
* ``3`` — no row failed, but at least one row is NO DATA: the gate table
  is INCOMPLETE and the 100-store commitment is not yet evidenced

MANUAL rows
-----------
§13's Security and Quality rows are settled by named test suites and
labelled sample sets (EPA A1/A2/A4 and D4), not by a fleet-run number.
Rather than invent a metric for them, this script reports them as
MANUAL and requires the operator to name the evidence in the
measurements file (``security_evidence`` / ``quality_evidence``). A
MANUAL row with no evidence string is NO DATA, not a pass.

Pass bars (all named, all overridable in the measurements file under
``thresholds``) come from the plan's task D1 step 3 and audit §13:
>= 99% terminal within 24 h, no unbounded queue, rollup < 30 min, pool
wait p95 < 100 ms, seven consecutive daily cycles, ~2x offered load.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any, Callable, Sequence

__all__ = [
    "DEFAULT_THRESHOLDS",
    "GATE_AREAS",
    "GateResult",
    "Verdict",
    "build_parser",
    "evaluate_gates",
    "exit_code_for",
    "main",
    "render_markdown",
    "render_text",
]


class Verdict(str):
    """One gate row's outcome. ``str`` subclass so it renders and
    JSON-serialises without a converter."""

    __slots__ = ()

    PASS: "Verdict"
    FAIL: "Verdict"
    NO_DATA: "Verdict"
    MANUAL: "Verdict"


Verdict.PASS = Verdict("PASS")
Verdict.FAIL = Verdict("FAIL")
Verdict.NO_DATA = Verdict("NO DATA")
Verdict.MANUAL = Verdict("MANUAL")


#: Every bar the table judges against. Sourced from plan task D1 step 3
#: ("Pass: >= 99% terminal within 24 h; no unbounded queue; rollup < 30
#: min; pool wait p95 < 100 ms") and audit §13 ("seven consecutive daily
#: cycles", "approximately 2x normal offered load").
DEFAULT_THRESHOLDS: dict[str, float] = {
    "terminal_fraction_min": 0.99,
    "daily_cycles_min": 7,
    "offered_load_multiplier_min": 2.0,
    "rollup_seconds_max": 1800.0,
    "pool_wait_p95_ms_max": 100.0,
    # The audit's own flagged regression: a 55-minute due-to-first-attempt
    # startup gap it asks to investigate. A run that reproduces it has not
    # cleared the tail-latency gate.
    "due_to_dispatch_p95_seconds_max": 3300.0,
    "queue_oldest_pending_seconds_max": 86400.0,
    # A7/D3's agreed reconciliation band for ledger-vs-provider bytes.
    "cost_model_drift_ratio_min": 0.9,
    "cost_model_drift_ratio_max": 1.1,
    # Deep dive §10's assumption is 1.5 physical attempts per logical
    # check; 3.0 is double that and is the point at which retry
    # amplification, not the workload, is setting the bill.
    "attempts_per_valid_fresh_max": 3.0,
    # A scorecard day that could not measure more than a fifth of its
    # own columns is not evidence of anything.
    "missing_metric_fraction_max": 0.2,
}


@dataclass
class GateInputs:
    """Everything the table is derived from. Every field optional —
    absence is the normal state before the run happens, and it must
    read as NO DATA rather than as a zero."""

    #: `ScorecardRow`-shaped objects (or dicts) for the window, D5.
    scorecard: list[Any] = field(default_factory=list)
    #: `BaselineMetrics`-shaped object (or dict), A5.
    baseline: Any = None
    #: Operator-supplied step-3 figures.
    measurements: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    def measure(self, key: str) -> Any:
        value = self.measurements.get(key)
        return None if value == "" else value

    def bar(self, key: str) -> float:
        return float(self.thresholds.get(key, DEFAULT_THRESHOLDS[key]))

    def baseline_value(self, key: str) -> Any:
        if self.baseline is None:
            return None
        if isinstance(self.baseline, dict):
            return self.baseline.get(key)
        return getattr(self.baseline, key, None)

    def phase_p95(self, phase: str) -> float | None:
        phases = self.baseline_value("target_phase_p95_seconds") or {}
        value = phases.get(phase) if isinstance(phases, dict) else None
        return None if value is None else float(value)

    def scorecard_values(self, key: str) -> list[float]:
        """Every non-``None`` value of ``key`` across the window."""
        out: list[float] = []
        for row in self.scorecard:
            value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
            if value is not None:
                out.append(float(value))
        return out


@dataclass(frozen=True)
class GateResult:
    """One rendered row of the §13 table."""

    area: str
    verdict: Verdict
    detail: str
    evidence_required: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "verdict": str(self.verdict),
            "detail": self.detail,
            "evidence_required": self.evidence_required,
        }


def _no_data(*missing: str) -> tuple[Verdict, str]:
    return Verdict.NO_DATA, "not measured: " + ", ".join(missing)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


# --------------------------------------------------------------------------
# One evaluator per §13 area
# --------------------------------------------------------------------------


def _daily_freshness(i: GateInputs) -> tuple[Verdict, str]:
    fraction = i.measure("terminal_fraction_24h")
    cycles = i.measure("daily_cycles_completed")
    missing = [
        name
        for name, value in (
            ("terminal_fraction_24h", fraction),
            ("daily_cycles_completed", cycles),
        )
        if value is None
    ]
    if missing:
        return _no_data(*missing)
    fraction = float(fraction)
    cycles = int(cycles)
    bar = i.bar("terminal_fraction_min")
    cycles_bar = int(i.bar("daily_cycles_min"))
    ok = fraction >= bar and cycles >= cycles_bar
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        f"terminal within 24 h = {fraction:.4f} (bar {bar}); "
        f"consecutive daily cycles = {cycles} (bar {cycles_bar})",
    )


def _headroom(i: GateInputs) -> tuple[Verdict, str]:
    multiplier = i.measure("offered_load_multiplier")
    start = i.measure("queue_depth_start")
    end = i.measure("queue_depth_end")
    missing = [
        name
        for name, value in (
            ("offered_load_multiplier", multiplier),
            ("queue_depth_start", start),
            ("queue_depth_end", end),
        )
        if value is None
    ]
    if missing:
        return _no_data(*missing)
    oldest = i.baseline_value("queue_oldest_pending_seconds")
    bounded = float(end) <= float(start)
    oldest_ok = oldest is None or float(oldest) <= i.bar(
        "queue_oldest_pending_seconds_max"
    )
    ok = float(multiplier) >= i.bar("offered_load_multiplier_min") and bounded and oldest_ok
    detail = (
        f"offered load = {float(multiplier):.2f}x (bar "
        f"{i.bar('offered_load_multiplier_min')}x); queue depth "
        f"{_fmt(start)} -> {_fmt(end)}; oldest pending {_fmt(oldest)} s"
    )
    if not bounded:
        detail += " — QUEUE GREW under 2x load"
    return (Verdict.PASS if ok else Verdict.FAIL, detail)


def _tail_latency(i: GateInputs) -> tuple[Verdict, str]:
    phases = {
        phase: i.phase_p95(phase)
        for phase in (
            "due_to_dispatch",
            "dispatch_to_first_network",
            "first_network_to_persisted",
        )
    }
    missing = [name for name, value in phases.items() if value is None]
    if missing:
        return _no_data(*(f"crawmatic_target_phase_p95_seconds[{m}]" for m in missing))
    bar = i.bar("due_to_dispatch_p95_seconds_max")
    ok = phases["due_to_dispatch"] <= bar
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        "p95 s: "
        + ", ".join(f"{name}={value:.1f}" for name, value in phases.items())
        + f" (due_to_dispatch bar {bar:.0f} s — audit's 55-min startup gap)",
    )


def _fault_rows(i: GateInputs, rows: Sequence[str], label: str) -> tuple[Verdict, str]:
    """Verdicts for a subset of the D2 fault-injection matrix.

    ``measurements["fault_injection"]`` is ``{script name: "PASS"|"FAIL"}``,
    copied from the D2 scripts' own output
    (``tests/load/fault_injection/``, ``docs/ops/FAULT_INJECTION_2026-09.md``).
    """
    results = i.measure("fault_injection") or {}
    if not isinstance(results, dict):
        return _no_data(f"fault_injection ({label})")
    missing = [row for row in rows if row not in results]
    if missing:
        return _no_data(*(f"fault_injection[{row}]" for row in missing))
    verdicts = {row: str(results[row]).upper() for row in rows}
    ok = all(value == "PASS" for value in verdicts.values())
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        ", ".join(f"{row}={value}" for row, value in verdicts.items()),
    )


def _durability(i: GateInputs) -> tuple[Verdict, str]:
    return _fault_rows(
        i, ("kill_worker_after_post", "kill_scraper_after_fetch"), "durability"
    )


def _tenant_fairness(i: GateInputs) -> tuple[Verdict, str]:
    return _fault_rows(i, ("two_schedulers", "one_broken_tenant"), "tenant fairness")


def _host_protection(i: GateInputs) -> tuple[Verdict, str]:
    return _fault_rows(i, ("host_limit_hold",), "host protection")


def _manual(key: str, label: str) -> Callable[[GateInputs], tuple[Verdict, str]]:
    def evaluate(i: GateInputs) -> tuple[Verdict, str]:
        evidence = i.measure(key)
        if not evidence:
            return _no_data(key)
        return Verdict.MANUAL, f"{label}: {evidence}"

    return evaluate


def _economics(i: GateInputs) -> tuple[Verdict, str]:
    drift = i.baseline_value("cost_model_drift_ratio")
    attempts = i.baseline_value("attempts_per_valid_fresh_24h")
    if attempts is None:
        attempts_values = i.scorecard_values("attempts_per_valid_fresh")
        attempts = max(attempts_values) if attempts_values else None
    cost_values = i.scorecard_values("cost_per_valid_fresh_micro_usd")
    missing: list[str] = []
    if drift is None:
        missing.append("cost_model_drift_ratio (no provider usage imported)")
    if attempts is None:
        missing.append("attempts_per_valid_fresh")
    if not cost_values:
        missing.append("cost_per_valid_fresh_micro_usd (scorecard)")
    if missing:
        return _no_data(*missing)
    drift = float(drift)
    attempts = float(attempts)
    ok = (
        i.bar("cost_model_drift_ratio_min") <= drift <= i.bar("cost_model_drift_ratio_max")
        and attempts <= i.bar("attempts_per_valid_fresh_max")
    )
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        f"ledger/provider drift = {drift:.4f} (band "
        f"{i.bar('cost_model_drift_ratio_min')}-{i.bar('cost_model_drift_ratio_max')}); "
        f"attempts per valid fresh = {attempts:.2f} (bar "
        f"{i.bar('attempts_per_valid_fresh_max')}); cost per valid fresh "
        f"= {max(cost_values):.0f} micro-USD (max over window)",
    )


def _database(i: GateInputs) -> tuple[Verdict, str]:
    rollup = i.measure("rollup_seconds")
    pool_wait = i.measure("pool_wait_p95_ms")
    retention = i.measure("retention_dry_run_seconds")
    migration = i.measure("alembic_upgrade_seconds")
    incomplete_deletions = i.measure("retention_deletions_on_incomplete_rollup")
    missing = [
        name
        for name, value in (
            ("rollup_seconds", rollup),
            ("pool_wait_p95_ms", pool_wait),
            ("retention_dry_run_seconds", retention),
            ("alembic_upgrade_seconds", migration),
            ("retention_deletions_on_incomplete_rollup", incomplete_deletions),
        )
        if value is None
    ]
    if missing:
        return _no_data(*missing)
    ok = (
        float(rollup) < i.bar("rollup_seconds_max")
        and float(pool_wait) < i.bar("pool_wait_p95_ms_max")
        and int(incomplete_deletions) == 0
    )
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        f"rollup {float(rollup):.0f} s (bar {i.bar('rollup_seconds_max'):.0f} s); "
        f"pool wait p95 {float(pool_wait):.1f} ms (bar "
        f"{i.bar('pool_wait_p95_ms_max'):.0f} ms); retention dry run "
        f"{float(retention):.0f} s; alembic upgrade {float(migration):.0f} s; "
        f"deletions on incomplete rollup = {int(incomplete_deletions)}",
    )


def _recovery(i: GateInputs) -> tuple[Verdict, str]:
    rpo = i.measure("restore_rpo_seconds")
    rto = i.measure("restore_rto_seconds")
    backup_bytes = i.measure("backup_export_bytes")
    backup_seconds = i.measure("backup_export_seconds")
    missing = [
        name
        for name, value in (
            ("restore_rpo_seconds", rpo),
            ("restore_rto_seconds", rto),
            ("backup_export_bytes", backup_bytes),
            ("backup_export_seconds", backup_seconds),
        )
        if value is None
    ]
    if missing:
        return _no_data(*missing)
    agreed_rpo = i.measurements.get("agreed_rpo_seconds")
    agreed_rto = i.measurements.get("agreed_rto_seconds")
    if agreed_rpo is None or agreed_rto is None:
        return (
            Verdict.NO_DATA,
            "restore measured (RPO "
            f"{float(rpo):.0f} s, RTO {float(rto):.0f} s, export "
            f"{int(backup_bytes)} B in {float(backup_seconds):.0f} s) but no "
            "agreed_rpo_seconds/agreed_rto_seconds to judge against — the audit "
            "requires an AGREED target, which is an owner decision, not a default",
        )
    ok = float(rpo) <= float(agreed_rpo) and float(rto) <= float(agreed_rto)
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        f"RPO {float(rpo):.0f}/{float(agreed_rpo):.0f} s; "
        f"RTO {float(rto):.0f}/{float(agreed_rto):.0f} s; export "
        f"{int(backup_bytes)} B in {float(backup_seconds):.0f} s",
    )


def _operations(i: GateInputs) -> tuple[Verdict, str]:
    heartbeats = i.measure("heartbeat_process_classes_reporting")
    expected = i.measure("heartbeat_process_classes_expected")
    alerts = i.measure("alert_destination_configured")
    fractions = i.scorecard_values("missing_metric_fraction")
    missing = [
        name
        for name, value in (
            ("heartbeat_process_classes_reporting", heartbeats),
            ("heartbeat_process_classes_expected", expected),
            ("alert_destination_configured", alerts),
        )
        if value is None
    ]
    if not fractions:
        missing.append("scorecard rows for the window")
    if missing:
        return _no_data(*missing)
    worst = max(fractions)
    ok = (
        int(heartbeats) >= int(expected)
        and bool(alerts)
        and worst <= i.bar("missing_metric_fraction_max")
    )
    return (
        Verdict.PASS if ok else Verdict.FAIL,
        f"heartbeats {int(heartbeats)}/{int(expected)}; alert destination "
        f"configured = {bool(alerts)}; worst scorecard missing-metric fraction "
        f"= {worst:.3f} (bar {i.bar('missing_metric_fraction_max')})",
    )


#: The §13 table, in the audit's own order and wording.
GATE_AREAS: tuple[tuple[str, str, Callable[[GateInputs], tuple[Verdict, str]]], ...] = (
    (
        "Daily freshness",
        "Seven consecutive daily cycles at target catalogue size; >= 99% of "
        "eligible matches reach a valid terminal outcome within the window.",
        _daily_freshness,
    ),
    (
        "Headroom",
        "~2x normal offered load for a bounded interval without unbounded "
        "queues or DB collapse.",
        _headroom,
    ),
    (
        "Tail latency",
        "due-to-first-attempt and due-to-valid-price p50/p95/p99 tracked; the "
        "observed 55-minute startup gap investigated.",
        _tail_latency,
    ),
    (
        "Durability",
        "Kill worker after accepted dispatch and scraper after successful "
        "fetch; recover without unbounded duplicate work or lost observations.",
        _durability,
    ),
    (
        "Tenant fairness",
        "A blocked or oversized store cannot prevent another store's due work; "
        "concurrent schedulers do not fire the same occurrence twice.",
        _tenant_fairness,
    ),
    (
        "Host protection",
        "Fleet host/provider limits hold across replicas, retry paths and "
        "lease expiry.",
        _host_protection,
    ),
    (
        "Security",
        "Exact-image redirect, subresource/private-DNS, worker/popup and regex "
        "tests pass; grants minimised; live cross-tenant RLS test passes.",
        _manual("security_evidence", "evidence"),
    ),
    (
        "Quality",
        "Labelled per-domain samples cover variant identity, currency, seller, "
        "discounts, shipping, unavailable products and conflicting candidates.",
        _manual("quality_evidence", "evidence"),
    ),
    (
        "Economics",
        "Physical ledger totals reconcile to provider/container measurements "
        "within tolerance; cost per valid refresh and retry amplification "
        "within approved bounds.",
        _economics,
    ),
    (
        "Database",
        "Realistic-volume rollups, retention, pool exhaustion, migration and "
        "backup tests pass; no deletion on incomplete rollup coverage.",
        _database,
    ),
    (
        "Recovery",
        "Restore DB plus configuration/buffers/evidence from off-host "
        "material; demonstrate agreed RPO/RTO at projected size.",
        _recovery,
    ),
    (
        "Operations",
        "Heartbeats configured; freshness, queue age, persistence backlog, "
        "stale breaker evaluation, budget denials, disk and restore failures "
        "alert to an owner.",
        _operations,
    ),
)


def evaluate_gates(inputs: GateInputs) -> list[GateResult]:
    """Every §13 row, judged. Never raises: an evaluator that blows up
    becomes NO DATA naming the exception, because a crashed gate script
    must not be mistaken for a clean table."""
    results: list[GateResult] = []
    for area, requirement, evaluate in GATE_AREAS:
        try:
            verdict, detail = evaluate(inputs)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            verdict, detail = Verdict.NO_DATA, f"evaluator error: {type(exc).__name__}"
        results.append(
            GateResult(
                area=area,
                verdict=verdict,
                detail=detail,
                evidence_required=requirement,
            )
        )
    return results


def exit_code_for(results: Sequence[GateResult]) -> int:
    """``1`` if anything failed, else ``3`` if anything is unmeasured,
    else ``0``. An incomplete table is never a success."""
    if any(r.verdict == Verdict.FAIL for r in results):
        return 1
    if any(r.verdict == Verdict.NO_DATA for r in results):
        return 3
    return 0


def render_markdown(results: Sequence[GateResult], *, window: str) -> str:
    lines = [
        f"| Area | Verdict | Measurement ({window}) |",
        "|---|---|---|",
    ]
    for result in results:
        detail = result.detail.replace("|", "\\|")
        lines.append(f"| {result.area} | {result.verdict} | {detail} |")
    return "\n".join(lines)


def render_text(results: Sequence[GateResult], *, window: str) -> str:
    width = max(len(r.area) for r in results)
    lines = [f"audit §13 release gates — window {window}", ""]
    for result in results:
        lines.append(f"{result.area:<{width}}  {str(result.verdict):<7}  {result.detail}")
    counts: dict[str, int] = {}
    for result in results:
        counts[str(result.verdict)] = counts.get(str(result.verdict), 0) + 1
    lines.append("")
    lines.append("  ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Live input collection
# --------------------------------------------------------------------------


def collect_inputs(
    *,
    database_url: str | None,
    since: date_type,
    until: date_type,
    measurements: dict[str, Any],
) -> GateInputs:
    """Read the scorecard + baseline gauges if a DSN was given.

    A DB that cannot be read degrades the DB-sourced rows to NO DATA and
    leaves the operator-supplied rows intact — the same
    per-group-degrades-independently rule ``collect_baseline_metrics``
    itself follows.
    """
    thresholds = dict(DEFAULT_THRESHOLDS)
    override = measurements.get("thresholds")
    if isinstance(override, dict):
        thresholds.update({k: float(v) for k, v in override.items()})

    inputs = GateInputs(measurements=measurements, thresholds=thresholds)
    if not database_url:
        return inputs

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app_shared.maintenance.scorecard import read_scorecard_range
    from app_shared.opsmetrics.emit import collect_baseline_metrics

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            try:
                inputs.scorecard = list(read_scorecard_range(session, since, until))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"warning: scorecard unreadable ({type(exc).__name__}); "
                    "its rows will read NO DATA",
                    file=sys.stderr,
                )
                session.rollback()
            try:
                inputs.baseline = collect_baseline_metrics(session)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"warning: baseline metrics unreadable ({type(exc).__name__}); "
                    "their rows will read NO DATA",
                    file=sys.stderr,
                )
    finally:
        engine.dispose()
    return inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet_test_report.py",
        description=(
            "Emit the audit §13 release-gate table with pass/fail for a fleet-test "
            "run window. Exit 0 all clear, 1 a gate failed, 3 the table is incomplete."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "DSN to read the D5 scorecard and A5 baseline gauges from. Omit to "
            "build the table from the measurements file alone."
        ),
    )
    parser.add_argument(
        "--measurements",
        default=None,
        help=(
            "JSON file of step-3 figures (see docs/ops/FLEET_TEST_2026-09.md). "
            "Anything absent reads NO DATA — never a pass."
        ),
    )
    parser.add_argument("--since", default=None, help="Window start, YYYY-MM-DD (UTC).")
    parser.add_argument("--until", default=None, help="Window end, YYYY-MM-DD (UTC).")
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="markdown is the shape docs/ops/FLEET_TEST_2026-09.md wants.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    measurements: dict[str, Any] = {}
    if args.measurements:
        try:
            with open(args.measurements, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"REFUSED: cannot read --measurements: {exc}", file=sys.stderr)
            return 2
        if not isinstance(loaded, dict):
            print("REFUSED: --measurements must contain a JSON object.", file=sys.stderr)
            return 2
        measurements = loaded

    today = datetime.now(timezone.utc).date()
    try:
        until = date_type.fromisoformat(args.until) if args.until else today
        since = (
            date_type.fromisoformat(args.since) if args.since else until - timedelta(days=7)
        )
    except ValueError as exc:
        print(f"REFUSED: bad --since/--until: {exc}", file=sys.stderr)
        return 2
    if since > until:
        print("REFUSED: --since is after --until.", file=sys.stderr)
        return 2

    inputs = collect_inputs(
        database_url=args.database_url,
        since=since,
        until=until,
        measurements=measurements,
    )
    results = evaluate_gates(inputs)
    window = f"{since.isoformat()}..{until.isoformat()}"

    if args.format == "json":
        print(
            json.dumps(
                {
                    "window": {"since": since.isoformat(), "until": until.isoformat()},
                    "scorecard_days": len(inputs.scorecard),
                    "gates": [r.as_dict() for r in results],
                },
                indent=2,
            )
        )
    elif args.format == "markdown":
        print(render_markdown(results, window=window))
    else:
        print(render_text(results, window=window))

    return exit_code_for(results)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
