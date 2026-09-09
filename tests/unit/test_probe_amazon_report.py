"""Report-math tests for scripts/probe_amazon_http_leg.py (EPA C2, F08).

Every test here works on fixture :class:`ProbeAttempt` rows -- no database
session, no network, no Amazon fetch, no proxy traffic. That mirrors the
script's own contract: the live fetch loop (Step 2) is a spend step and is
never run by this suite (ASSUMPTIONS.md answer 3); only the pure report-math
core is exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# `scripts/` has no __init__.py — same sys.path convention as
# tests/unit/test_canary_document_only_browser.py / test_run_gate_d_canary.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.probe_amazon_http_leg import (  # noqa: E402
    DEFAULT_DOMAIN,
    DIRECT_LEG_COST_ESTIMATE_USD,
    HTTP_PRICE_SUCCESS_HIGH,
    HTTP_PRICE_SUCCESS_LOW,
    PROXIED_LEG_COST_ESTIMATE_USD,
    ProbeAttempt,
    SpendCapExceeded,
    SpendGuard,
    build_parser,
    classify_decision,
    classify_probe_attempt,
    estimate_run_cost_usd,
    report_to_dict,
    summarize_attempts,
    summarize_leg,
)


def _attempt(
    *,
    leg: str,
    run: int = 1,
    target: str = "t1",
    status: int | None = 200,
    title_present: bool = True,
    price: float | None = 19.99,
    classification: str | None = None,
    wire_bytes: int | None = 12_000,
) -> ProbeAttempt:
    return ProbeAttempt(
        target=target,
        leg=leg,
        run=run,
        status=status,
        title_present=title_present,
        price=price,
        classification=classification,
        wire_bytes=wire_bytes,
    )


# --- classify_probe_attempt --------------------------------------------------


def test_classify_probe_attempt_success_is_none():
    assert classify_probe_attempt(status_code=200, title_present=True, price_found=True) is None


def test_classify_probe_attempt_http_status_wins_over_extraction():
    result = classify_probe_attempt(status_code=403, title_present=False, price_found=False)
    assert result == "HTTP_403"


def test_classify_probe_attempt_no_identity_is_extraction_failed():
    result = classify_probe_attempt(status_code=200, title_present=False, price_found=False)
    assert result == "EXTRACTION_FAILED"


def test_classify_probe_attempt_identity_but_no_price_is_price_not_found():
    result = classify_probe_attempt(status_code=200, title_present=True, price_found=False)
    assert result == "PRICE_NOT_FOUND"


def test_classify_probe_attempt_exception_delegates_to_classify_exception():
    result = classify_probe_attempt(
        status_code=None, title_present=False, price_found=False,
        exception=TimeoutError("timed out"),
    )
    assert result == "TIMEOUT"


def test_classify_probe_attempt_no_status_no_exception_is_unknown():
    result = classify_probe_attempt(status_code=None, title_present=False, price_found=False)
    assert result == "UNKNOWN_ERROR"


# --- summarize_leg ------------------------------------------------------------


def test_summarize_leg_empty_is_all_zero_not_fabricated():
    summary = summarize_leg([])
    assert summary.attempts == 0
    assert summary.price_success_pct == 0.0
    assert summary.title_present_pct == 0.0
    assert summary.wire_bytes_avg == 0.0
    assert summary.classification_counts == {}


def test_summarize_leg_counts_price_success_and_title_present_independently():
    attempts = [
        _attempt(leg="direct", price=10.0, title_present=True, classification=None),
        _attempt(leg="direct", price=None, title_present=True, classification="PRICE_NOT_FOUND"),
        _attempt(leg="direct", price=None, title_present=False, classification="EXTRACTION_FAILED"),
        _attempt(leg="direct", price=5.0, title_present=True, classification=None),
    ]
    summary = summarize_leg(attempts)
    assert summary.attempts == 4
    assert summary.price_success == 2
    assert summary.price_success_pct == 50.0
    assert summary.title_present_pct == 75.0
    assert summary.classification_counts == {"OK": 2, "PRICE_NOT_FOUND": 1, "EXTRACTION_FAILED": 1}


def test_summarize_leg_wire_bytes_average_ignores_unmeasured_attempts():
    attempts = [
        _attempt(leg="proxied", wire_bytes=10_000),
        _attempt(leg="proxied", wire_bytes=30_000),
        _attempt(leg="proxied", wire_bytes=None, status=None, classification="TIMEOUT", price=None),
    ]
    summary = summarize_leg(attempts)
    assert summary.attempts == 3
    assert summary.wire_bytes_measured == 2
    assert summary.wire_bytes_total == 40_000
    assert summary.wire_bytes_avg == 20_000.0


# --- classify_decision (the plan's decision table, verbatim) -----------------


@pytest.mark.parametrize(
    "pct,expected_case",
    [
        (100.0, "http_first_browser_fallback_capped_1"),
        (80.0, "http_first_browser_fallback_capped_1"),  # inclusive boundary
        (79.9, "http_first_for_http_works_subset"),
        (30.0, "http_first_for_http_works_subset"),  # inclusive boundary
        (29.9, "browser_first_with_5pct_recovery_probe"),
        (0.0, "browser_first_with_5pct_recovery_probe"),
    ],
)
def test_classify_decision_matches_plan_thresholds(pct, expected_case):
    case, detail = classify_decision(pct)
    assert case == expected_case
    assert f"{pct:.1f}%" in detail


def test_decision_boundaries_match_module_constants():
    assert HTTP_PRICE_SUCCESS_HIGH == 80.0
    assert HTTP_PRICE_SUCCESS_LOW == 30.0


# --- summarize_attempts (the full report) -------------------------------------


def test_summarize_attempts_splits_by_leg_and_computes_combined_decision():
    attempts = [
        # direct: 3/4 price success = 75%
        _attempt(leg="direct", target="t1", price=10.0),
        _attempt(leg="direct", target="t2", price=10.0),
        _attempt(leg="direct", target="t3", price=10.0),
        _attempt(leg="direct", target="t4", price=None, title_present=False,
                  classification="EXTRACTION_FAILED"),
        # proxied: 4/4 price success = 100%
        _attempt(leg="proxied", target="t1", price=10.0),
        _attempt(leg="proxied", target="t2", price=10.0),
        _attempt(leg="proxied", target="t3", price=10.0),
        _attempt(leg="proxied", target="t4", price=10.0),
    ]
    report = summarize_attempts(attempts, domain=DEFAULT_DOMAIN, targets=4, runs=1, max_usd=1.0)
    assert set(report.legs) == {"direct", "proxied"}
    assert report.legs["direct"].price_success_pct == 75.0
    assert report.legs["proxied"].price_success_pct == 100.0
    # combined: 7/8 = 87.5%
    assert report.overall_price_success_pct == 87.5
    assert report.decision_case == "http_first_browser_fallback_capped_1"


def test_summarize_attempts_absent_leg_is_not_reported():
    attempts = [_attempt(leg="direct")]
    report = summarize_attempts(attempts, domain=DEFAULT_DOMAIN, targets=1, runs=1, max_usd=1.0)
    assert set(report.legs) == {"direct"}
    assert "proxied" not in report.legs


def test_summarize_attempts_no_attempts_reports_zero_and_lowest_band():
    report = summarize_attempts([], domain=DEFAULT_DOMAIN, targets=50, runs=3, max_usd=1.0)
    assert report.overall_price_success_pct == 0.0
    assert report.decision_case == "browser_first_with_5pct_recovery_probe"
    assert report.legs == {}


def test_report_to_dict_is_json_serializable_and_carries_every_field():
    attempts = [_attempt(leg="direct"), _attempt(leg="proxied", price=None, title_present=False,
                                                    classification="EXTRACTION_FAILED")]
    report = summarize_attempts(
        attempts, domain=DEFAULT_DOMAIN, targets=1, runs=1, max_usd=1.0,
        generated_at="2026-09-08T00:00:00+00:00",
    )
    payload = report_to_dict(report)
    encoded = json.dumps(payload)  # must not raise
    decoded = json.loads(encoded)
    assert decoded["domain"] == DEFAULT_DOMAIN
    assert decoded["generated_at"] == "2026-09-08T00:00:00+00:00"
    assert decoded["legs"]["direct"]["price_success"] == 1
    assert decoded["legs"]["proxied"]["classification_counts"] == {"EXTRACTION_FAILED": 1}
    assert decoded["decision_case"] in {
        "http_first_browser_fallback_capped_1",
        "http_first_for_http_works_subset",
        "browser_first_with_5pct_recovery_probe",
    }


# --- SpendGuard / cost estimate -----------------------------------------------


def test_spend_guard_allows_charges_within_cap():
    guard = SpendGuard(max_usd=1.00)
    guard.charge(0.40, reason="test")
    guard.charge(0.40, reason="test")
    assert guard.spent_usd == pytest.approx(0.80)


def test_spend_guard_refuses_a_charge_that_would_exceed_the_cap():
    guard = SpendGuard(max_usd=1.00)
    guard.charge(0.90, reason="test")
    with pytest.raises(SpendCapExceeded):
        guard.charge(0.20, reason="test")
    # the refused charge must not have been recorded
    assert guard.spent_usd == pytest.approx(0.90)


def test_estimate_run_cost_usd_direct_leg_is_free():
    cost = estimate_run_cost_usd(targets=50, runs=3, legs=("direct",))
    assert cost == 0.0
    assert DIRECT_LEG_COST_ESTIMATE_USD == 0.0


def test_estimate_run_cost_usd_proxied_leg_uses_the_documented_rate():
    cost = estimate_run_cost_usd(targets=50, runs=3, legs=("proxied",))
    assert cost == pytest.approx(50 * 3 * PROXIED_LEG_COST_ESTIMATE_USD)


def test_estimate_run_cost_usd_both_legs_sums_per_leg():
    cost = estimate_run_cost_usd(targets=10, runs=1, legs=("direct", "proxied"))
    assert cost == pytest.approx(10 * (DIRECT_LEG_COST_ESTIMATE_USD + PROXIED_LEG_COST_ESTIMATE_USD))


# --- CLI: --max-usd is required, and the report/decision plumbing wires up ---


def test_max_usd_is_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--targets", "50", "--runs", "3", "--report", "out.json"])


def test_max_usd_and_targets_parse_through():
    parser = build_parser()
    args = parser.parse_args(["--targets", "50", "--runs", "3", "--max-usd", "1.00", "--report", "out.json"])
    assert args.targets == 50
    assert args.runs == 3
    assert args.max_usd == 1.00
    assert args.report == "out.json"


def test_dry_run_writes_a_report_without_opening_a_database(tmp_path, monkeypatch):
    from scripts import probe_amazon_http_leg as probe

    def _poisoned(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("--dry-run opened a database session")

    monkeypatch.setattr(probe, "_open_session", _poisoned)
    out = tmp_path / "report.json"

    exit_code = probe.main(
        ["--dry-run", "--targets", "50", "--runs", "3", "--max-usd", "1.00", "--report", str(out)]
    )
    assert exit_code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"].startswith("DRY RUN")
    assert "--max-usd 1.00" in written["deferred_live_command"]


def test_no_workspace_also_takes_the_dry_path_and_spends_nothing(tmp_path, monkeypatch):
    from scripts import probe_amazon_http_leg as probe

    def _poisoned(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("no --workspace but a database session was opened anyway")

    monkeypatch.setattr(probe, "_open_session", _poisoned)
    out = tmp_path / "report.json"
    exit_code = probe.main(["--targets", "5", "--runs", "1", "--max-usd", "0.01", "--report", str(out)])
    assert exit_code == 0
    assert out.exists()
