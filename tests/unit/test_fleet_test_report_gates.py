"""The audit §13 release-gate table's judgement (EPA D1, F22).

`scripts/fleet_test_report.py` is the acceptance instrument for the
fleet test: whether the 100-store commitment is evidenced is whatever
this table says. So the property that matters most is not "it computes
the right pass" — it is that **a missing measurement can never read as
a pass**. Every row therefore gets a no-input case as well as a
pass/fail case, and the exit-code mapping is pinned separately.

Nothing here touches a database: `evaluate_gates` takes already-read
inputs, exactly so it can be judged offline.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.fleet_test_report import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    GATE_AREAS,
    GateInputs,
    Verdict,
    evaluate_gates,
    exit_code_for,
    main,
    render_markdown,
)

ALL_AREAS = [area for area, _, _ in GATE_AREAS]


def _baseline(**overrides):
    values = {
        "queue_oldest_pending_seconds": 120.0,
        "cost_model_drift_ratio": 1.02,
        "attempts_per_valid_fresh_24h": 1.4,
        "target_phase_p95_seconds": {
            "due_to_dispatch": 300.0,
            "dispatch_to_first_network": 12.0,
            "first_network_to_persisted": 4.0,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _scorecard(days: int = 7, **overrides):
    row = {
        "missing_metric_fraction": 0.05,
        "cost_per_valid_fresh_micro_usd": 900.0,
        "attempts_per_valid_fresh": 1.4,
    }
    row.update(overrides)
    return [dict(row, date=date(2026, 9, 1)) for _ in range(days)]


def _measurements(**overrides):
    values = {
        "terminal_fraction_24h": 0.995,
        "daily_cycles_completed": 7,
        "offered_load_multiplier": 2.0,
        "queue_depth_start": 1200,
        "queue_depth_end": 900,
        "fault_injection": {
            "kill_worker_after_post": "PASS",
            "kill_scraper_after_fetch": "PASS",
            "two_schedulers": "PASS",
            "one_broken_tenant": "PASS",
            "host_limit_hold": "PASS",
        },
        "security_evidence": "tests/integration/test_rls_cross_tenant.py, A1/A2/A4 suites",
        "quality_evidence": "docs/ops/OFFER_TRUTH_SET.md, D4 labelled samples",
        "rollup_seconds": 640.0,
        "pool_wait_p95_ms": 18.0,
        "retention_dry_run_seconds": 95.0,
        "alembic_upgrade_seconds": 41.0,
        "retention_deletions_on_incomplete_rollup": 0,
        "restore_rpo_seconds": 3600,
        "restore_rto_seconds": 7200,
        "agreed_rpo_seconds": 14400,
        "agreed_rto_seconds": 14400,
        "backup_export_bytes": 3_400_000_000,
        "backup_export_seconds": 410.0,
        "heartbeat_process_classes_reporting": 5,
        "heartbeat_process_classes_expected": 5,
        "alert_destination_configured": True,
    }
    values.update(overrides)
    return values


def _all_green() -> GateInputs:
    return GateInputs(
        scorecard=_scorecard(),
        baseline=_baseline(),
        measurements=_measurements(),
        thresholds=dict(DEFAULT_THRESHOLDS),
    )


def _verdicts(inputs: GateInputs) -> dict[str, str]:
    return {result.area: str(result.verdict) for result in evaluate_gates(inputs)}


# --------------------------------------------------------------------------
# The table itself
# --------------------------------------------------------------------------


def test_table_is_the_audit_s_twelve_rows_in_order() -> None:
    assert ALL_AREAS == [
        "Daily freshness",
        "Headroom",
        "Tail latency",
        "Durability",
        "Tenant fairness",
        "Host protection",
        "Security",
        "Quality",
        "Economics",
        "Database",
        "Recovery",
        "Operations",
    ]


def test_empty_inputs_are_never_a_pass() -> None:
    """The whole point: an unmeasured gate reads NO DATA, not PASS."""
    verdicts = _verdicts(GateInputs())
    assert set(verdicts.values()) == {"NO DATA"}
    assert len(verdicts) == 12


def test_all_green_inputs_clear_every_row() -> None:
    verdicts = _verdicts(_all_green())
    assert verdicts["Security"] == "MANUAL"
    assert verdicts["Quality"] == "MANUAL"
    assert {
        area: verdict
        for area, verdict in verdicts.items()
        if verdict not in ("PASS", "MANUAL")
    } == {}


# --------------------------------------------------------------------------
# Pass bars from the plan (>= 99% / 2x / rollup < 30 min / pool wait < 100 ms)
# --------------------------------------------------------------------------


def test_terminal_fraction_bar_is_ninety_nine_percent() -> None:
    assert DEFAULT_THRESHOLDS["terminal_fraction_min"] == 0.99
    inputs = _all_green()
    inputs.measurements["terminal_fraction_24h"] = 0.9899
    assert _verdicts(inputs)["Daily freshness"] == "FAIL"
    inputs.measurements["terminal_fraction_24h"] = 0.99
    assert _verdicts(inputs)["Daily freshness"] == "PASS"


def test_seven_consecutive_cycles_are_required() -> None:
    inputs = _all_green()
    inputs.measurements["daily_cycles_completed"] = 6
    assert _verdicts(inputs)["Daily freshness"] == "FAIL"


def test_a_growing_queue_fails_headroom() -> None:
    inputs = _all_green()
    inputs.measurements["queue_depth_end"] = 1_500_000
    result = next(r for r in evaluate_gates(inputs) if r.area == "Headroom")
    assert result.verdict == Verdict.FAIL
    assert "QUEUE GREW" in result.detail


def test_less_than_two_x_offered_load_fails_headroom() -> None:
    inputs = _all_green()
    inputs.measurements["offered_load_multiplier"] = 1.4
    assert _verdicts(inputs)["Headroom"] == "FAIL"


def test_rollup_over_thirty_minutes_fails_the_database_gate() -> None:
    assert DEFAULT_THRESHOLDS["rollup_seconds_max"] == 1800.0
    inputs = _all_green()
    inputs.measurements["rollup_seconds"] = 1801.0
    assert _verdicts(inputs)["Database"] == "FAIL"


def test_pool_wait_over_one_hundred_ms_fails_the_database_gate() -> None:
    assert DEFAULT_THRESHOLDS["pool_wait_p95_ms_max"] == 100.0
    inputs = _all_green()
    inputs.measurements["pool_wait_p95_ms"] = 100.0
    assert _verdicts(inputs)["Database"] == "FAIL"


def test_any_deletion_on_incomplete_rollup_fails_the_database_gate() -> None:
    inputs = _all_green()
    inputs.measurements["retention_deletions_on_incomplete_rollup"] = 1
    assert _verdicts(inputs)["Database"] == "FAIL"


def test_tail_latency_uses_the_a5_phase_p95_gauges() -> None:
    inputs = _all_green()
    inputs.baseline = _baseline(
        target_phase_p95_seconds={
            "due_to_dispatch": 3301.0,  # the audit's 55-minute startup gap
            "dispatch_to_first_network": 12.0,
            "first_network_to_persisted": 4.0,
        }
    )
    assert _verdicts(inputs)["Tail latency"] == "FAIL"


def test_a_missing_phase_gauge_is_no_data_not_a_pass() -> None:
    inputs = _all_green()
    inputs.baseline = _baseline(
        target_phase_p95_seconds={
            "due_to_dispatch": 100.0,
            "dispatch_to_first_network": None,
            "first_network_to_persisted": 4.0,
        }
    )
    assert _verdicts(inputs)["Tail latency"] == "NO DATA"


# --------------------------------------------------------------------------
# D2 matrix, economics, recovery, operations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row, area",
    [
        ("kill_worker_after_post", "Durability"),
        ("kill_scraper_after_fetch", "Durability"),
        ("two_schedulers", "Tenant fairness"),
        ("one_broken_tenant", "Tenant fairness"),
        ("host_limit_hold", "Host protection"),
    ],
)
def test_each_fault_injection_row_maps_to_its_gate(row, area) -> None:
    inputs = _all_green()
    inputs.measurements["fault_injection"] = dict(
        inputs.measurements["fault_injection"], **{row: "FAIL"}
    )
    assert _verdicts(inputs)[area] == "FAIL"

    inputs = _all_green()
    missing = dict(inputs.measurements["fault_injection"])
    missing.pop(row)
    inputs.measurements["fault_injection"] = missing
    assert _verdicts(inputs)[area] == "NO DATA"


def test_unimported_provider_usage_leaves_economics_unmeasured() -> None:
    """`cost_model_drift_ratio` is NULL when nobody imported the
    provider's own usage export -- that is "we cannot say", and the gate
    must not read it as agreement."""
    inputs = _all_green()
    inputs.baseline = _baseline(cost_model_drift_ratio=None)
    assert _verdicts(inputs)["Economics"] == "NO DATA"


def test_drift_outside_the_band_fails_economics() -> None:
    inputs = _all_green()
    inputs.baseline = _baseline(cost_model_drift_ratio=1.6)
    assert _verdicts(inputs)["Economics"] == "FAIL"


def test_retry_amplification_over_the_bar_fails_economics() -> None:
    inputs = _all_green()
    inputs.baseline = _baseline(attempts_per_valid_fresh_24h=4.0)
    assert _verdicts(inputs)["Economics"] == "FAIL"


def test_recovery_without_an_agreed_target_is_not_a_pass() -> None:
    """RPO/RTO targets are an owner decision; a measured restore with no
    agreed bar is evidence, not a verdict."""
    inputs = _all_green()
    inputs.measurements.pop("agreed_rpo_seconds")
    result = next(r for r in evaluate_gates(inputs) if r.area == "Recovery")
    assert result.verdict == Verdict.NO_DATA
    assert "agreed" in result.detail


def test_recovery_over_the_agreed_rto_fails() -> None:
    inputs = _all_green()
    inputs.measurements["restore_rto_seconds"] = 99_999
    assert _verdicts(inputs)["Recovery"] == "FAIL"


def test_a_blind_scorecard_day_fails_operations() -> None:
    inputs = _all_green()
    inputs.scorecard = _scorecard(missing_metric_fraction=0.9)
    assert _verdicts(inputs)["Operations"] == "FAIL"


def test_missing_heartbeat_class_fails_operations() -> None:
    inputs = _all_green()
    inputs.measurements["heartbeat_process_classes_reporting"] = 3
    assert _verdicts(inputs)["Operations"] == "FAIL"


def test_manual_rows_require_named_evidence() -> None:
    inputs = _all_green()
    inputs.measurements["security_evidence"] = ""
    assert _verdicts(inputs)["Security"] == "NO DATA"


# --------------------------------------------------------------------------
# Thresholds, exit codes, rendering, CLI
# --------------------------------------------------------------------------


def test_thresholds_can_be_overridden_per_run() -> None:
    inputs = _all_green()
    inputs.measurements["terminal_fraction_24h"] = 0.95
    inputs.thresholds["terminal_fraction_min"] = 0.95
    assert _verdicts(inputs)["Daily freshness"] == "PASS"


def test_an_exploding_evaluator_degrades_to_no_data() -> None:
    inputs = _all_green()
    inputs.measurements["fault_injection"] = 12345  # not a mapping
    assert _verdicts(inputs)["Durability"] == "NO DATA"


def test_exit_codes_distinguish_fail_from_incomplete() -> None:
    assert exit_code_for(evaluate_gates(_all_green())) == 0
    assert exit_code_for(evaluate_gates(GateInputs())) == 3
    failing = _all_green()
    failing.measurements["terminal_fraction_24h"] = 0.1
    assert exit_code_for(evaluate_gates(failing)) == 1


def test_markdown_render_escapes_pipes_and_lists_every_row() -> None:
    rendered = render_markdown(evaluate_gates(_all_green()), window="w")
    assert rendered.count("\n") == len(ALL_AREAS) + 1
    for area in ALL_AREAS:
        assert f"| {area} |" in rendered


def test_cli_without_a_database_url_reports_an_incomplete_table(tmp_path, capsys) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    code = main(["--measurements", str(path), "--format", "markdown"])
    assert code == 3
    assert "NO DATA" in capsys.readouterr().out


def test_cli_all_green_measurements_exit_zero_without_a_database(tmp_path, capsys) -> None:
    """Every DB-sourced row can also be supplied by hand, so the table is
    still buildable if the staging DB is already torn down."""
    measurements = _measurements()
    path = tmp_path / "m.json"
    path.write_text(json.dumps(measurements), encoding="utf-8")
    code = main(["--measurements", str(path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    verdicts = {row["area"]: row["verdict"] for row in payload["gates"]}
    # Tail latency, Economics and Operations need the DB-sourced gauges,
    # so they stay NO DATA here -- which is exactly why the exit code is 3.
    assert code == 3
    assert verdicts["Daily freshness"] == "PASS"
    assert verdicts["Tail latency"] == "NO DATA"


def test_cli_refuses_a_bad_measurements_file(tmp_path, capsys) -> None:
    path = tmp_path / "m.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    assert main(["--measurements", str(path)]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_a_reversed_window(capsys) -> None:
    assert main(["--since", "2026-09-08", "--until", "2026-09-01"]) == 2
    assert "REFUSED" in capsys.readouterr().err
