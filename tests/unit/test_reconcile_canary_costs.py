"""EPA D3: unit coverage for `scripts/reconcile_canary_costs.py`'s
reconciliation math — the explicit acceptance criterion ("Unit coverage
exists for the reconciliation math on fixture inputs").

Every test below constructs a `ReconciliationInputs` by hand (a plain
dataclass of numbers) and calls the pure `compute_reconciliation`
function — no database, no Redis, no Railway API, no subprocess. This is
also what `--dry-run` exercises end to end, so `test_dry_run_cli_*`
covers the CLI wiring the same way the fixture tests cover the math.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.reconcile_canary_costs import (  # noqa: E402
    BYTES_TOLERANCE_PCT,
    CPU_TOLERANCE_PCT,
    DRIFT_RATIO_MAX,
    DRIFT_RATIO_MIN,
    ReconciliationInputs,
    build_parser,
    compute_reconciliation,
    main,
)


def _inputs(**overrides) -> ReconciliationInputs:
    defaults = dict(
        job_id="job-1",
        ledger_bytes=10_000_000,
        provider_bytes=10_000_000,
        modeled_browser_cpu_seconds=200.0,
        measured_cpu_vcpu_minutes=200.0 / 60.0,
        ledger_cost_usd=1.0,
        max_usd=3.0,
    )
    defaults.update(overrides)
    return ReconciliationInputs(**defaults)


# --- bytes tolerance --------------------------------------------------------


def test_bytes_exact_match_passes():
    verdict = compute_reconciliation(_inputs())
    assert verdict.bytes_within_tolerance
    assert verdict.bytes_drift_ratio == pytest.approx(1.0)


def test_bytes_at_tolerance_edge_passes():
    # ledger 10% over provider is exactly the +10% edge.
    verdict = compute_reconciliation(
        _inputs(ledger_bytes=11_000_000, provider_bytes=10_000_000)
    )
    assert verdict.bytes_within_tolerance


def test_bytes_just_over_tolerance_fails():
    verdict = compute_reconciliation(
        _inputs(ledger_bytes=11_000_001, provider_bytes=10_000_000)
    )
    assert not verdict.bytes_within_tolerance
    assert not verdict.passed
    assert any("bytes ratio" in reason for reason in verdict.reasons)


def test_bytes_under_tolerance_fails():
    verdict = compute_reconciliation(
        _inputs(ledger_bytes=8_000_000, provider_bytes=10_000_000)
    )
    assert not verdict.bytes_within_tolerance


def test_provider_bytes_zero_is_unevaluable_not_a_false_pass():
    verdict = compute_reconciliation(_inputs(provider_bytes=0))
    assert verdict.bytes_drift_ratio is None
    assert not verdict.bytes_within_tolerance
    assert not verdict.passed
    assert any("provider_bytes is zero" in reason for reason in verdict.reasons)


# --- CPU tolerance -----------------------------------------------------------


def test_cpu_exact_match_passes():
    verdict = compute_reconciliation(_inputs())
    assert verdict.cpu_within_tolerance
    assert verdict.cpu_drift_ratio == pytest.approx(1.0)


def test_cpu_at_tolerance_edge_passes():
    # modeled 25% over measured is exactly the +25% edge.
    verdict = compute_reconciliation(
        _inputs(modeled_browser_cpu_seconds=250.0, measured_cpu_vcpu_minutes=200.0 / 60.0)
    )
    assert verdict.cpu_within_tolerance


def test_cpu_just_over_tolerance_fails():
    verdict = compute_reconciliation(
        _inputs(modeled_browser_cpu_seconds=250.01, measured_cpu_vcpu_minutes=200.0 / 60.0)
    )
    assert not verdict.cpu_within_tolerance
    assert not verdict.passed


def test_measured_cpu_zero_is_unevaluable_not_a_false_pass():
    verdict = compute_reconciliation(_inputs(measured_cpu_vcpu_minutes=0.0))
    assert verdict.cpu_drift_ratio is None
    assert not verdict.cpu_within_tolerance
    assert not verdict.passed


def test_measured_cpu_seconds_converts_minutes_correctly():
    verdict = compute_reconciliation(
        _inputs(modeled_browser_cpu_seconds=120.0, measured_cpu_vcpu_minutes=2.0)
    )
    assert verdict.measured_cpu_seconds == pytest.approx(120.0)
    assert verdict.cpu_drift_ratio == pytest.approx(1.0)


# --- cost_model_drift_ratio pass bar (tighter than the ops-alerting band) ---


def test_drift_ratio_within_pass_bar():
    verdict = compute_reconciliation(_inputs())
    assert verdict.cost_model_drift_within_pass_bar
    assert DRIFT_RATIO_MIN <= verdict.cost_model_drift_ratio <= DRIFT_RATIO_MAX


def test_drift_ratio_outside_pass_bar_but_inside_ops_alert_band_still_fails():
    # 1.3 is comfortably inside the 0.5-2.0 ops-alerting band but well
    # outside this canary's own 0.9-1.1 bar — the whole point of having a
    # tighter, separate bar for a controlled single-job canary.
    verdict = compute_reconciliation(
        _inputs(ledger_bytes=13_000_000, provider_bytes=10_000_000)
    )
    assert not verdict.cost_model_drift_within_pass_bar
    assert not verdict.passed


# --- spend gate --------------------------------------------------------------


def test_spend_within_cap_passes():
    verdict = compute_reconciliation(_inputs(ledger_cost_usd=2.99, max_usd=3.0))
    assert verdict.spend_within_cap


def test_spend_exceeding_cap_fails():
    verdict = compute_reconciliation(_inputs(ledger_cost_usd=3.01, max_usd=3.0))
    assert not verdict.spend_within_cap
    assert not verdict.passed
    assert any("exceeds --max-usd" in reason for reason in verdict.reasons)


def test_spend_exactly_at_cap_passes():
    verdict = compute_reconciliation(_inputs(ledger_cost_usd=3.0, max_usd=3.0))
    assert verdict.spend_within_cap


# --- overall passed requires every gate ------------------------------------


def test_passed_requires_all_four_gates():
    good = _inputs()
    assert compute_reconciliation(good).passed

    bad_bytes = _inputs(ledger_bytes=20_000_000)
    assert not compute_reconciliation(bad_bytes).passed

    bad_cpu = _inputs(modeled_browser_cpu_seconds=1000.0)
    assert not compute_reconciliation(bad_cpu).passed

    bad_spend = _inputs(ledger_cost_usd=100.0)
    assert not compute_reconciliation(bad_spend).passed


# --- custom tolerances (constructor defaults still match module constants) --


def test_default_tolerances_match_module_constants():
    assert BYTES_TOLERANCE_PCT == 10.0
    assert CPU_TOLERANCE_PCT == 25.0
    assert DRIFT_RATIO_MIN == 0.9
    assert DRIFT_RATIO_MAX == 1.1


def test_custom_tolerance_widens_pass_window():
    tight = compute_reconciliation(
        _inputs(ledger_bytes=11_500_000, provider_bytes=10_000_000)
    )
    assert not tight.bytes_within_tolerance

    widened = compute_reconciliation(
        _inputs(ledger_bytes=11_500_000, provider_bytes=10_000_000),
        bytes_tolerance_pct=20.0,
    )
    assert widened.bytes_within_tolerance


# --- CLI: the spend gate refuses without --max-usd --------------------------


def test_cli_refuses_without_max_usd(capsys):
    exit_code = main(["--dry-run"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "max-usd" in captured.err


def test_cli_refuses_zero_max_usd(capsys):
    exit_code = main(["--dry-run", "--max-usd", "0"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err


def test_cli_refuses_negative_max_usd(capsys):
    exit_code = main(["--dry-run", "--max-usd", "-1"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err


def test_cli_refuses_live_run_without_job_id_or_database_url(capsys):
    exit_code = main(["--max-usd", "3"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "job-id" in captured.err


def test_dry_run_cli_passes(capsys):
    exit_code = main(["--dry-run", "--max-usd", "3"])
    captured = capsys.readouterr()
    assert "verdict=PASS" in captured.out
    assert exit_code == 0


def test_build_parser_has_required_flags():
    parser = build_parser()
    actions = {action.dest for action in parser._actions}
    assert {"job_id", "provider_window", "max_usd", "database_url", "dry_run"} <= actions
