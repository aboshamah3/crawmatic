#!/usr/bin/env python3
"""Render `/srv/crawmatic/evidence/w55ga-load-2026-08-26/REPORT.md` from the
JSON results `run_load_suite.sh` collected (EPA W5.5-GA item B, report §10).

Not itself a "load" script — a pure formatter, invoked by
`run_load_suite.sh` after every scenario has run. Kept in `tests/load/` so
the whole harness is self-contained in one directory.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path

DEV_SERVER_BOUND_LABEL = (
    "DEV-SERVER-BOUND — measured on this one dev box under docker, no other "
    "tenant traffic, no production hardware. Never quote as a production "
    "budget without re-measurement on production-shaped infrastructure."
)


def parse_scenario_status(raw: str) -> dict[str, str]:
    status = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        name, _, value = line.partition("=")
        status[name] = value
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--total-seconds", required=True, type=int)
    parser.add_argument("--scenario-status", required=True)
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    status = parse_scenario_status(args.scenario_status)

    scenario_names = [
        "noisy_tenant",
        "hot_domain",
        "large_catalog",
        "scheduler_backlog",
        "pool_exhaustion",
        "n1_detection",
        "browser_saturation",
    ]

    payloads: dict[str, dict] = {}
    for name in scenario_names:
        json_path = results_dir / f"{name}.json"
        if json_path.exists():
            payloads[name] = json.loads(json_path.read_text())

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []
    lines.append("# W5.5-GA — Load Test Report")
    lines.append("")
    lines.append(f"Generated {now}. EPA task W5.5-GA, item B (report §10).")
    lines.append("")
    lines.append(f"**{DEV_SERVER_BOUND_LABEL}**")
    lines.append("")
    lines.append(
        f"Total suite wall time: **{args.total_seconds}s** "
        f"({args.total_seconds / 60:.1f} min) — bounded, minutes not hours, "
        "per the task's constraint."
    )
    lines.append("")
    lines.append("## Scenario status")
    lines.append("")
    lines.append("| Scenario | Script exit | Needs DB |")
    lines.append("|---|---|---|")
    needs_db = {
        "noisy_tenant": "no",
        "hot_domain": "no",
        "large_catalog": "no",
        "scheduler_backlog": "no",
        "pool_exhaustion": "yes",
        "n1_detection": "yes",
        "browser_saturation": "N/A (skipped-here)",
    }
    script_names = {
        "noisy_tenant": "scenario_noisy_tenant.py",
        "hot_domain": "scenario_hot_domain.py",
        "large_catalog": "scenario_large_catalog.py",
        "scheduler_backlog": "scenario_scheduler_backlog.py",
        "pool_exhaustion": "scenario_pool_exhaustion.py",
        "n1_detection": "scenario_n1_detection.py",
        "browser_saturation": "scenario_browser_saturation.py",
    }
    for name in scenario_names:
        exit_status = status.get(script_names[name], "unknown")
        lines.append(f"| {name} | {exit_status} | {needs_db[name]} |")
    lines.append("")

    lines.append("## Findings summary")
    lines.append("")
    for name in scenario_names:
        payload = payloads.get(name)
        if not payload:
            lines.append(f"- **{name}**: no result file — see the suite log.")
            continue
        finding = payload.get("finding", payload.get("reason", "(no finding recorded)"))
        lines.append(f"- **{name}**: {finding}")
    lines.append("")

    lines.append("## Per-scenario detail")
    lines.append("")
    for name in scenario_names:
        payload = payloads.get(name)
        lines.append(f"### {name}")
        lines.append("")
        if not payload:
            lines.append("No result file produced — see the suite log for this scenario.")
            lines.append("")
            continue
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2, default=str))
        lines.append("```")
        lines.append("")

    lines.append("## Budgets / headroom (dev-bound numbers only)")
    lines.append("")
    lc = payloads.get("large_catalog")
    if lc:
        for run in lc.get("runs", []):
            lines.append(
                f"- `plan_batches` at {run['targets']} targets: "
                f"{run['elapsed_seconds']}s ({run['targets_per_second']}/s), dev-bound."
            )
    sb = payloads.get("scheduler_backlog")
    if sb:
        lines.append(
            f"- Scheduler backlog drain: {sb.get('total_backlog')} items across "
            f"{sb.get('tenant_count')} tenants needed {sb.get('passes_run')} pass(es) "
            f"at FLEET_CONCURRENCY={sb.get('fleet_concurrency_cap')} "
            f"(fully_drained={sb.get('fully_drained')})."
        )
    pe = payloads.get("pool_exhaustion")
    if pe:
        lines.append(
            f"- Pool exhaustion: {pe.get('pool_capacity')}-connection bounded pool, "
            f"{pe.get('concurrent_workers')} concurrent callers -> "
            f"{pe.get('workers_acquired')} acquired, {pe.get('workers_pool_timeout')} "
            f"clean pool_timeout, {pe.get('workers_other_error')} unexpected errors."
        )
    n1 = payloads.get("n1_detection")
    if n1:
        lines.append(
            f"- N+1: {n1.get('target')} — "
            f"{n1.get('statements_per_additional_pair')} SQL statements per "
            f"additional (workspace, variant) pair (n_plus_one_confirmed="
            f"{n1.get('n_plus_one_confirmed')})."
        )
    lines.append("")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
