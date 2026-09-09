"""Unit tests for the parent/child/price/provider-aware calculator added to
`scripts/canary_document_only_browser.py` (EPA A6, deep dive §8.1).

The old `PageRow`/`summarize_phase` pair (still covered by
`test_canary_document_only_browser.py`, unchanged) treats every
`network_operations` row as one page. On a real job that counts a 10-second
browser navigation plus its ~99 subresource children as "100 pages" with a
fabricated 0 ms latency and a byte average diluted 100x. `summarize` below
is the fix: it tells a PAGE (`parent_operation_id is None`) from a child
attached to one, keeps missing bytes/durations `None` rather than coercing
them to `0`, and refuses a comparison that does not clear a minimum
coverage bar.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.canary_document_only_browser import (  # noqa: E402
    MAX_UNKNOWN_PCT,
    MIN_COMPARABLE_PARENTS,
    CalculatorSummary,
    PageOperation,
    calculator_summary_to_phase_summary,
    evaluate_coverage,
    page_operation_from_mapping,
    summarize,
)


def op(
    *,
    parent: str | None,
    id: str,
    duration_ms: int | None,
    bytes: int | None,
    price_ok: bool | None,
    provider: str | None,
    proxy_provider_id: str | None = None,
) -> PageOperation:
    """Builds one `PageOperation` — the deep dive's reproduction shape."""
    return PageOperation(
        network_request_id=id,
        parent_operation_id=parent,
        provider=provider,
        proxy_provider_id=proxy_provider_id,
        duration_ms=duration_ms,
        bytes_compressed=bytes,
        price_ok=price_ok,
    )


def _page(id: str = "p1", **overrides) -> PageOperation:
    defaults = dict(
        parent=None, id=id, duration_ms=1_000, bytes=100_000, price_ok=True, provider="dataimpulse"
    )
    defaults.update(overrides)
    return op(**defaults)


# --- the deep dive's reproduction --------------------------------------------


def test_calculator_counts_parent_pages_and_attaches_children():
    rows = [op(parent=None, id="p1", duration_ms=10_000, bytes=200_000, price_ok=False, provider="dataimpulse")]
    rows += [
        op(parent="p1", id=f"c{i}", duration_ms=None, bytes=20_000, price_ok=None, provider="dataimpulse")
        for i in range(99)
    ]
    s = summarize(rows)
    assert s.pages == 1 and s.price_success == 0 and s.p50_ms == 10_000
    assert s.bytes_per_page == pytest.approx(2_180_000) and s.unknown_durations == 0


def test_old_bug_would_have_reported_100_pages_and_zero_latency():
    # Documents exactly what the deep dive measured as wrong, so a future
    # regression that reintroduces per-row counting fails loudly here too.
    rows = [op(parent=None, id="p1", duration_ms=10_000, bytes=200_000, price_ok=False, provider="dataimpulse")]
    rows += [
        op(parent="p1", id=f"c{i}", duration_ms=None, bytes=20_000, price_ok=None, provider="dataimpulse")
        for i in range(99)
    ]
    s = summarize(rows)
    assert s.pages != 100
    assert s.p50_ms != 0
    assert s.bytes_per_page != pytest.approx(21_800)


# --- pages vs. children -------------------------------------------------------


def test_multiple_pages_each_with_their_own_children():
    rows = [
        _page("p1", duration_ms=5_000, bytes=100_000, price_ok=True),
        op(parent="p1", id="c1", duration_ms=None, bytes=50_000, price_ok=None, provider="dataimpulse"),
        _page("p2", duration_ms=15_000, bytes=300_000, price_ok=False),
        op(parent="p2", id="c2", duration_ms=None, bytes=150_000, price_ok=None, provider="dataimpulse"),
    ]
    s = summarize(rows)
    assert s.pages == 2
    assert s.price_success == 1
    assert s.p50_ms == 5_000  # nearest-rank of [5000, 15000] at the 50th percentile
    assert s.bytes_per_page == pytest.approx((150_000 + 450_000) / 2)


def test_a_child_naming_an_unknown_parent_is_never_counted_as_a_page():
    rows = [op(parent="does-not-exist", id="orphan", duration_ms=1, bytes=1, price_ok=None, provider="p")]
    s = summarize(rows)
    assert s.pages == 0
    assert s.bytes_per_page == 0.0


def test_children_attach_by_parent_id_regardless_of_their_own_provider():
    # A CDN subresource served by a different provider than the page's own
    # proxy must still count toward that page's bytes total (deep dive
    # §8.1: "regardless of destination hostname").
    rows = [
        _page("p1", duration_ms=1_000, bytes=100_000, price_ok=True, provider="dataimpulse"),
        op(parent="p1", id="cdn1", duration_ms=None, bytes=900_000, price_ok=None, provider="cloudfront-cdn"),
    ]
    s = summarize(rows)
    assert s.pages == 1
    assert s.bytes_per_page == pytest.approx(1_000_000)
    assert set(s.providers) == {"dataimpulse", "cloudfront-cdn"}


# --- price outcome -------------------------------------------------------------


def test_price_success_only_counts_pages_not_children():
    rows = [
        _page("p1", price_ok=True),
        _page("p2", price_ok=True),
        _page("p3", price_ok=False),
        # A child with price_ok=True (should never happen, but must not be
        # counted even if it did — price is a page-level outcome).
        op(parent="p1", id="c1", duration_ms=None, bytes=1, price_ok=True, provider="x"),
    ]
    s = summarize(rows)
    assert s.pages == 3
    assert s.price_success == 2


def test_price_ok_none_is_not_a_success():
    rows = [_page("p1", price_ok=None)]
    s = summarize(rows)
    assert s.price_success == 0


# --- unknowns are preserved, never coerced to zero ----------------------------


def test_unknown_durations_and_bytes_are_counted_not_zeroed():
    rows = [
        _page("p1", duration_ms=None, bytes=None, price_ok=True),
        _page("p2", duration_ms=5_000, bytes=None, price_ok=True),
        _page("p3", duration_ms=None, bytes=100_000, price_ok=True),
    ]
    s = summarize(rows)
    assert s.pages == 3
    assert s.unknown_durations == 2
    assert s.unknown_bytes == 2
    # The one known duration still drives the percentile — an unknown page
    # is excluded from the ranking, not folded in as a 0.
    assert s.p50_ms == 5_000
    # Only the one known byte figure contributes to the total.
    assert s.bytes_per_page == pytest.approx(100_000 / 3)


def test_no_operations_measured_is_zero_everywhere_not_a_crash():
    s = summarize([])
    assert s.pages == 0
    assert s.price_success == 0
    assert s.p50_ms == 0.0
    assert s.bytes_per_page == 0.0
    assert s.unknown_durations == 0
    assert s.unknown_bytes == 0
    assert s.providers == ()


# --- row -> PageOperation mapping ---------------------------------------------


def test_page_operation_from_mapping_preserves_none_as_none():
    row = page_operation_from_mapping(
        {
            "network_request_id": "abc",
            "parent_operation_id": None,
            "provider": "dataimpulse",
            "proxy_provider_id": None,
            "duration_ms": None,
            "bytes_compressed": None,
            "price_ok": None,
        }
    )
    assert row == PageOperation(
        network_request_id="abc",
        parent_operation_id=None,
        provider="dataimpulse",
        proxy_provider_id=None,
        duration_ms=None,
        bytes_compressed=None,
        price_ok=None,
    )


def test_page_operation_from_mapping_reads_a_child_row():
    row = page_operation_from_mapping(
        {
            "network_request_id": "c1",
            "parent_operation_id": "p1",
            "provider": "dataimpulse",
            "proxy_provider_id": "11111111-1111-1111-1111-111111111111",
            "duration_ms": 250,
            "bytes_compressed": 4_096,
            "price_ok": False,
        }
    )
    assert row.parent_operation_id == "p1"
    assert row.proxy_provider_id == "11111111-1111-1111-1111-111111111111"
    assert row.duration_ms == 250
    assert row.price_ok is False


# --- coverage refusal ----------------------------------------------------------


def _summary(**overrides) -> CalculatorSummary:
    defaults = dict(
        pages=100,
        price_success=95,
        p50_ms=1_000.0,
        p95_ms=2_000.0,
        bytes_per_page=100_000.0,
        unknown_durations=0,
        unknown_bytes=0,
        providers=("dataimpulse",),
    )
    defaults.update(overrides)
    return CalculatorSummary(**defaults)


def test_coverage_ok_with_plenty_of_pages_and_no_unknowns():
    verdict = evaluate_coverage(_summary(), _summary())
    assert verdict.ok is True
    assert verdict.reasons == ()


def test_coverage_refuses_fewer_than_fifty_parents():
    assert MIN_COMPARABLE_PARENTS == 50
    verdict = evaluate_coverage(_summary(pages=49), _summary())
    assert verdict.ok is False
    assert any("49" in reason for reason in verdict.reasons)


def test_coverage_refuses_more_than_five_percent_unknowns():
    assert MAX_UNKNOWN_PCT == 5.0
    # 6 unknown out of 100 pages = 6% > the 5% ceiling.
    verdict = evaluate_coverage(_summary(unknown_durations=6), _summary())
    assert verdict.ok is False


def test_coverage_allows_exactly_five_percent_unknowns():
    verdict = evaluate_coverage(_summary(unknown_durations=5), _summary())
    assert verdict.ok is True


def test_coverage_refuses_different_target_sets():
    verdict = evaluate_coverage(
        _summary(),
        _summary(),
        baseline_target_ids=frozenset({"m1", "m2"}),
        candidate_target_ids=frozenset({"m1", "m3"}),
    )
    assert verdict.ok is False
    assert any("target set" in reason for reason in verdict.reasons)


def test_coverage_allows_identical_target_sets():
    verdict = evaluate_coverage(
        _summary(),
        _summary(),
        baseline_target_ids=frozenset({"m1", "m2"}),
        candidate_target_ids=frozenset({"m1", "m2"}),
    )
    assert verdict.ok is True


def test_coverage_refuses_different_strategy_profile_ids():
    verdict = evaluate_coverage(
        _summary(),
        _summary(),
        baseline_strategy_profile_id="profile-a",
        candidate_strategy_profile_id="profile-b",
    )
    assert verdict.ok is False
    assert any("strategy profile" in reason for reason in verdict.reasons)


def test_coverage_allows_matching_strategy_profile_ids():
    verdict = evaluate_coverage(
        _summary(),
        _summary(),
        baseline_strategy_profile_id="profile-a",
        candidate_strategy_profile_id="profile-a",
    )
    assert verdict.ok is True


def test_coverage_with_unresolved_profile_on_either_side_does_not_refuse():
    # `load_phase_dimensions` returns `None` when a phase mixed more than
    # one profile / recorded none — that is a "we don't know", not a
    # mismatch, and must not itself sink an otherwise-good comparison.
    verdict = evaluate_coverage(
        _summary(),
        _summary(),
        baseline_strategy_profile_id=None,
        candidate_strategy_profile_id="profile-a",
    )
    assert verdict.ok is True


# --- bridging into the existing PhaseSummary/report pipeline ------------------


def test_calculator_summary_bridges_into_phase_summary_for_the_existing_report():
    calc = _summary(pages=50, price_success=45, bytes_per_page=200_000.0)
    phase = calculator_summary_to_phase_summary("A baseline", calc, policy_version=2)
    assert phase.label == "A baseline"
    assert phase.policy_version == 2
    assert phase.pages == 50
    assert phase.successes == 45
    assert phase.success_pct == pytest.approx(90.0)
    assert phase.proxy_bytes_per_page == pytest.approx(200_000.0)
    assert phase.proxy_bytes_total == 50 * 200_000


def test_calculator_summary_bridge_handles_zero_pages():
    calc = _summary(pages=0, price_success=0, bytes_per_page=0.0)
    phase = calculator_summary_to_phase_summary("empty", calc, policy_version=2)
    assert phase.success_pct == 0.0
    assert phase.proxy_bytes_total == 0
