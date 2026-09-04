"""Unit tests for `scripts/canary_document_only_browser.py` (EPA B5).

The canary itself spends real money against a real merchant, so the only
things that can be tested here are the things that decide what that money
buys — and they are exactly the things that would silently ruin the run if
they were wrong:

1. **The acceptance computation.** Both rules must be able to FAIL
   independently, and the verdict must be the AND of them. A check that
   cannot fail reads as coverage while providing none.
2. **The percentile helper.** p50/p95 wall time is one of the two numbers
   the owner reads; an off-by-one in the rank moves the bar.
3. **The row -> page mapping.** A `network_operations` row becomes one page;
   "success" and "proxy bytes" have to mean the same thing in both phases or
   the comparison is between two different experiments.
4. **`--dry-run` really is dry.** It must produce the runbook and the
   acceptance rule with no database session and no network, so an
   orchestrator can run it unattended and a reviewer can read what the live
   run will do before anyone authorizes it.

Everything is driven from literals: no database, no network, no clock. The
dry-run test additionally poisons the session opener, so a regression that
made the dry path touch a database fails loudly here rather than in
production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `scripts/` has no __init__.py — same sys.path convention as
# tests/unit/test_run_gate_d_canary.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import canary_document_only_browser as canary  # noqa: E402
from scripts.canary_document_only_browser import (  # noqa: E402
    PROXY_BYTES_PER_PAGE_CEILING,
    SUCCESS_RATE_TOLERANCE_POINTS,
    PageRow,
    evaluate_acceptance,
    main,
    page_row_from_operation,
    percentile,
    render_report,
    summarize_phase,
)


def _rows(*, pages: int, successes: int, wall_ms: int, proxy_bytes: int) -> list[PageRow]:
    return [
        PageRow(success=index < successes, wall_ms=wall_ms, proxy_bytes=proxy_bytes)
        for index in range(pages)
    ]


# --- percentile helper --------------------------------------------------------


def test_percentile_nearest_rank_on_1_to_100():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95


def test_percentile_is_order_independent():
    assert percentile([9, 1, 5, 3, 7], 50) == 5


def test_percentile_of_a_single_value_is_that_value():
    assert percentile([1234], 50) == 1234
    assert percentile([1234], 95) == 1234


def test_percentile_of_nothing_measured_is_zero_not_a_crash():
    assert percentile([], 50) == 0.0
    assert percentile([], 95) == 0.0


# --- row -> page mapping ------------------------------------------------------


def test_successful_operation_row_maps_to_a_successful_page():
    row = page_row_from_operation(
        {"failure_reason": None, "response_status": 200, "duration_ms": 4200,
         "bytes_compressed": 300_000}
    )
    assert row == PageRow(success=True, wall_ms=4200, proxy_bytes=300_000)


def test_failed_operation_row_is_not_a_success():
    assert not page_row_from_operation(
        {"failure_reason": "TIMEOUT", "response_status": None, "duration_ms": 30_000,
         "bytes_compressed": 0}
    ).success


def test_non_2xx_operation_row_is_not_a_success():
    assert not page_row_from_operation(
        {"failure_reason": None, "response_status": 503, "duration_ms": 900,
         "bytes_compressed": 1_000}
    ).success


def test_unmeasured_bytes_count_as_zero_not_none():
    # A NULL `bytes_compressed` is "not measured"; treating it as None would
    # make the per-page average un-computable for the whole phase.
    row = page_row_from_operation(
        {"failure_reason": None, "response_status": 200, "duration_ms": 1, "bytes_compressed": None}
    )
    assert row.proxy_bytes == 0


# --- phase summary ------------------------------------------------------------


def test_summarize_phase_computes_rate_percentiles_and_bytes_per_page():
    rows = [
        PageRow(success=True, wall_ms=1000, proxy_bytes=100_000),
        PageRow(success=True, wall_ms=2000, proxy_bytes=200_000),
        PageRow(success=True, wall_ms=3000, proxy_bytes=300_000),
        PageRow(success=False, wall_ms=4000, proxy_bytes=400_000),
    ]

    summary = summarize_phase("baseline", rows, policy_version=2)

    assert summary.pages == 4
    assert summary.successes == 3
    assert summary.success_pct == 75.0
    assert summary.p50_wall_ms == 2000
    assert summary.p95_wall_ms == 4000
    assert summary.proxy_bytes_total == 1_000_000
    assert summary.proxy_bytes_per_page == 250_000


def test_summarize_phase_with_no_rows_is_zero_everywhere():
    summary = summarize_phase("baseline", [], policy_version=2)
    assert summary.pages == 0
    assert summary.success_pct == 0.0
    assert summary.proxy_bytes_per_page == 0.0


# --- acceptance ---------------------------------------------------------------


def test_acceptance_passes_when_success_holds_and_bytes_are_low():
    baseline = summarize_phase(
        "baseline", _rows(pages=50, successes=48, wall_ms=5000, proxy_bytes=2_600_000),
        policy_version=2,
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=50, successes=47, wall_ms=3000, proxy_bytes=300_000),
        policy_version=2,
    )

    result = evaluate_acceptance(baseline, candidate)

    assert result.accepted is True
    assert result.verdict == "ACCEPT"
    assert all(check.passed for check in result.checks)


def test_acceptance_fails_when_success_drops_more_than_three_points():
    baseline = summarize_phase(
        "baseline", _rows(pages=100, successes=96, wall_ms=5000, proxy_bytes=2_600_000),
        policy_version=2,
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=100, successes=92, wall_ms=3000, proxy_bytes=100_000),
        policy_version=2,
    )

    result = evaluate_acceptance(baseline, candidate)

    assert result.accepted is False
    assert result.verdict == "REJECT"
    assert [check.passed for check in result.checks] == [False, True]


def test_a_drop_of_exactly_the_tolerance_still_passes():
    # "within 3 points" includes 3 — the bar is stated in the report, so it
    # must not silently be "strictly less than 3".
    assert SUCCESS_RATE_TOLERANCE_POINTS == 3.0
    baseline = summarize_phase(
        "baseline", _rows(pages=100, successes=96, wall_ms=1, proxy_bytes=1), policy_version=2
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=100, successes=93, wall_ms=1, proxy_bytes=1),
        policy_version=2,
    )

    assert evaluate_acceptance(baseline, candidate).accepted is True


def test_a_higher_success_rate_never_fails_the_first_rule():
    baseline = summarize_phase(
        "baseline", _rows(pages=50, successes=40, wall_ms=1, proxy_bytes=1), policy_version=2
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=50, successes=50, wall_ms=1, proxy_bytes=1),
        policy_version=2,
    )

    assert evaluate_acceptance(baseline, candidate).checks[0].passed is True


def test_acceptance_fails_when_proxy_bytes_per_page_exceed_the_ceiling():
    assert PROXY_BYTES_PER_PAGE_CEILING == 400_000  # 0.4 MB
    baseline = summarize_phase(
        "baseline", _rows(pages=50, successes=48, wall_ms=1, proxy_bytes=2_600_000),
        policy_version=2,
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=50, successes=48, wall_ms=1, proxy_bytes=600_000),
        policy_version=2,
    )

    result = evaluate_acceptance(baseline, candidate)

    assert result.accepted is False
    assert [check.passed for check in result.checks] == [True, False]


def test_exactly_the_byte_ceiling_passes():
    baseline = summarize_phase(
        "baseline", _rows(pages=10, successes=10, wall_ms=1, proxy_bytes=2_600_000),
        policy_version=2,
    )
    candidate = summarize_phase(
        "document-only",
        _rows(pages=10, successes=10, wall_ms=1, proxy_bytes=PROXY_BYTES_PER_PAGE_CEILING),
        policy_version=2,
    )

    assert evaluate_acceptance(baseline, candidate).accepted is True


def test_an_empty_candidate_phase_can_never_be_accepted():
    # Zero pages measured means the run did not happen — a 0.0 MB/page
    # average must never read as "cheap enough, ship it".
    baseline = summarize_phase(
        "baseline", _rows(pages=50, successes=48, wall_ms=1, proxy_bytes=1), policy_version=2
    )
    candidate = summarize_phase("document-only", [], policy_version=2)

    assert evaluate_acceptance(baseline, candidate).accepted is False


# --- report rendering ---------------------------------------------------------


def test_report_carries_both_phases_the_rule_and_the_verdict():
    baseline = summarize_phase(
        "baseline", _rows(pages=50, successes=48, wall_ms=5000, proxy_bytes=2_600_000),
        policy_version=2,
    )
    candidate = summarize_phase(
        "document-only", _rows(pages=50, successes=47, wall_ms=3000, proxy_bytes=300_000),
        policy_version=2,
    )
    result = evaluate_acceptance(baseline, candidate)

    report = render_report(baseline=baseline, candidate=candidate, acceptance=result)

    assert "ACCEPT" in report
    assert "within 3" in report
    assert "0.4 MB" in report
    assert "96.0" in report  # baseline success rate
    assert "94.0" in report  # candidate success rate


def test_dry_run_report_says_it_measured_nothing():
    report = render_report(baseline=None, candidate=None, acceptance=None)
    assert "DRY RUN" in report
    assert "within 3" in report
    # The two-phase runbook is the whole point of the dry report: the
    # setting is read once per Scrapyd process, so phase B needs a redeploy.
    assert "BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS" in report


# --- CLI ----------------------------------------------------------------------


def test_dry_run_writes_the_report_without_a_database_or_network(tmp_path, monkeypatch):
    def _poisoned(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("--dry-run opened a database session")

    monkeypatch.setattr(canary, "_open_session", _poisoned)
    out = tmp_path / "REPORT.md"

    assert main(["--dry-run", "--out", str(out), "--workspace", "01a020de-871c-7760-8273-59f3b67d9c18"]) == 0

    written = out.read_text(encoding="utf-8")
    assert "DRY RUN" in written
    assert "BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS" in written


def test_dry_run_defaults_to_fifty_pages_and_the_plan_report_path():
    parser = canary.build_parser()
    args = parser.parse_args(["--dry-run"])
    assert args.n == 50
    assert Path(args.out) == Path("evidence/canary-document-only-2026-09/REPORT.md")


def test_a_bad_workspace_uuid_is_refused_at_parse_time():
    parser = canary.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--workspace", "not-a-uuid"])


def test_measuring_requires_both_phase_job_ids(tmp_path, monkeypatch):
    # Guard against half a comparison being written as if it were a result.
    def _poisoned(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("opened a database session with only one phase")

    monkeypatch.setattr(canary, "_open_session", _poisoned)
    with pytest.raises(SystemExit):
        main(["--baseline-job-id", "b" * 8, "--out", str(tmp_path / "R.md")])
