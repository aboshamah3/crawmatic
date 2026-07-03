"""Exhaustive pure alert-engine tests (SPEC-09 T015, contracts/alert-engine.md
"Determinism guarantees").

Every §23 branch (0-8), the exact boundary table, half-up rounding,
currency filtering, the empty-comparable no-div-by-zero path, the
defensive null-client-price guard (U1), NaN/Infinity/over-scale
rejection, the total severity map, and byte-identical reruns. No DB, no
framework — pure ``decimal``/``app_shared.enums`` only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app_shared.alerts.engine import (
    SEVERITY_BY_TYPE,
    AlertOutcome,
    CompetitorPrice,
    analyze,
    decide,
    discount_vs_average,
    filter_comparable,
    severity_for,
)
from app_shared.enums import AlertSeverity, AlertType

CUR = "SAR"


def _row(price, currency=CUR, success=True, comparable=True, match_id=None) -> CompetitorPrice:
    return CompetitorPrice(
        match_id=match_id or uuid.uuid4(),
        price=Decimal(price) if price is not None else None,
        currency=currency,
        success=success,
        comparable=comparable,
    )


# --- §23 branch coverage (steps 0-8) ----------------------------------------


def test_step0_null_client_price_is_defensive_and_does_not_raise() -> None:
    """U1: a null client price must degrade to NO_COMPETITOR_DATA, never raise."""
    alert_type, discount = decide(None, Decimal("10"), Decimal("10"), Decimal("10"), 3)
    assert alert_type is AlertType.NO_COMPETITOR_DATA
    assert discount is None


def test_step1_zero_comparable_count_is_no_competitor_data() -> None:
    alert_type, discount = decide(Decimal("100"), None, None, None, 0)
    assert alert_type is AlertType.NO_COMPETITOR_DATA
    assert discount is None


def test_step2_client_price_above_highest_is_risk_strict_gt() -> None:
    alert_type, _ = decide(Decimal("101"), Decimal("90"), Decimal("95"), Decimal("100"), 3)
    assert alert_type is AlertType.RISK


def test_step2_client_price_equal_to_highest_falls_through_not_risk() -> None:
    # Equal-to-highest must NOT trigger RISK (strict `>` only).
    alert_type, _ = decide(Decimal("100"), Decimal("90"), Decimal("95"), Decimal("100"), 3)
    assert alert_type is not AlertType.RISK


def test_step3_client_price_above_cheapest_is_high_price_strict_gt() -> None:
    alert_type, _ = decide(Decimal("95"), Decimal("90"), Decimal("100"), Decimal("110"), 3)
    assert alert_type is AlertType.HIGH_PRICE


def test_step3_client_price_equal_to_cheapest_falls_through_not_high_price() -> None:
    alert_type, _ = decide(Decimal("90"), Decimal("90"), Decimal("100"), Decimal("110"), 3)
    assert alert_type is not AlertType.HIGH_PRICE


def test_step5_discount_above_5_is_chance_to_increase_price() -> None:
    # average=100, client=90 -> discount = 10% > 5%.
    alert_type, discount = decide(Decimal("90"), Decimal("90"), Decimal("100"), Decimal("110"), 3)
    assert alert_type is AlertType.CHANCE_TO_INCREASE_PRICE
    assert discount == Decimal("10.0000")


def test_step6_discount_within_1_to_5_is_normal() -> None:
    # average=100, client=97 -> discount = 3%.
    alert_type, discount = decide(Decimal("97"), Decimal("97"), Decimal("100"), Decimal("110"), 3)
    assert alert_type is AlertType.NORMAL
    assert discount == Decimal("3.0000")


def test_step7_discount_below_1_is_close_to_competitors() -> None:
    # average=100, client=99.5 -> discount = 0.5%.
    alert_type, discount = decide(
        Decimal("99.5"), Decimal("99.5"), Decimal("100"), Decimal("110"), 3
    )
    assert alert_type is AlertType.CLOSE_TO_COMPETITORS
    assert discount == Decimal("0.5000")


def test_step8_unreachable_defensive_fallback_via_constructed_input() -> None:
    """Step 8 is unreachable from the real engine (client<=cheapest<=average
    implies discount >= 0); hand-construct cheapest > average so the
    quantized discount goes negative and hits the else-branch defensive
    HIGH_PRICE fallback."""
    # client=50, cheapest=60 (client <= cheapest, so no HIGH_PRICE at step 3),
    # average=40 -> discount = ((40-50)/40)*100 = -25% -> none of steps 5-7 match.
    alert_type, discount = decide(Decimal("50"), Decimal("60"), Decimal("40"), Decimal("110"), 3)
    assert alert_type is AlertType.HIGH_PRICE
    assert discount == Decimal("-25.0000")


# --- Boundary table (research D2) -------------------------------------------


def test_boundary_exactly_0_is_close_to_competitors() -> None:
    # average == client_price -> discount exactly 0.
    alert_type, discount = decide(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("110"), 3)
    assert discount == Decimal("0.0000")
    assert alert_type is AlertType.CLOSE_TO_COMPETITORS


def test_boundary_between_0_and_1_is_close_to_competitors() -> None:
    # average=100, client=99.9 -> discount = 0.1%.
    alert_type, discount = decide(Decimal("99.9"), Decimal("99.9"), Decimal("100"), Decimal("110"), 3)
    assert Decimal("0") < discount < Decimal("1")
    assert alert_type is AlertType.CLOSE_TO_COMPETITORS


def test_boundary_exactly_1_is_normal() -> None:
    # average=100, client=99 -> discount = 1%.
    alert_type, discount = decide(Decimal("99"), Decimal("99"), Decimal("100"), Decimal("110"), 3)
    assert discount == Decimal("1.0000")
    assert alert_type is AlertType.NORMAL


def test_boundary_between_1_and_5_is_normal() -> None:
    # average=100, client=97 -> discount = 3%.
    alert_type, discount = decide(Decimal("97"), Decimal("97"), Decimal("100"), Decimal("110"), 3)
    assert Decimal("1") < discount < Decimal("5")
    assert alert_type is AlertType.NORMAL


def test_boundary_exactly_5_is_normal() -> None:
    # average=100, client=95 -> discount = 5%.
    alert_type, discount = decide(Decimal("95"), Decimal("95"), Decimal("100"), Decimal("110"), 3)
    assert discount == Decimal("5.0000")
    assert alert_type is AlertType.NORMAL


def test_boundary_above_5_is_chance_to_increase_price() -> None:
    # average=100, client=90 -> discount = 10%.
    alert_type, discount = decide(Decimal("90"), Decimal("90"), Decimal("100"), Decimal("110"), 3)
    assert discount > Decimal("5")
    assert alert_type is AlertType.CHANCE_TO_INCREASE_PRICE


# --- Half-up rounding --------------------------------------------------------


def test_half_up_rounding_5th_decimal_5_rounds_up_before_compare() -> None:
    # Construct average/client such that the raw discount's 5th decimal is
    # exactly 5, forcing ROUND_HALF_UP to round up before the boundary
    # compare. average=3, client=2.99985 -> raw = ((3-2.99985)/3)*100
    # = (0.00015/3)*100 = 0.005 -> quantize(0.0001, HALF_UP) -> 0.0050.
    average = Decimal("3")
    client_price = Decimal("2.99985")
    d = discount_vs_average(average, client_price)
    assert d == Decimal("0.0050")


def test_half_up_rounding_rounds_5_up_not_down() -> None:
    # raw discount = 1.00005 -> should round to 1.0001 (half-up), not 1.0000.
    average = Decimal("200000")
    # (average - client)/average * 100 = 1.00005
    # => average - client = average * 1.00005 / 100
    client_price = average - (average * Decimal("1.00005") / Decimal(100))
    d = discount_vs_average(average, client_price)
    assert d == Decimal("1.0001")


# --- filter_comparable / currency filter ------------------------------------


def test_filter_comparable_includes_only_success_comparable_matching_currency_priced() -> None:
    included_row = _row("100")
    rows = [
        included_row,
        _row("50", success=False),  # excluded, not flagged mismatch
        _row("50", comparable=False),  # excluded, not flagged mismatch
        _row(None),  # excluded (price None), not flagged mismatch
    ]
    split = filter_comparable(CUR, rows)
    assert split.included_prices == [Decimal("100")]
    assert split.mismatched_match_ids == []


def test_filter_comparable_surfaces_currency_mismatched_ids() -> None:
    mismatched = _row("100", currency="USD")
    matching = _row("50", currency=CUR)
    split = filter_comparable(CUR, [mismatched, matching])
    assert split.included_prices == [Decimal("50")]
    assert split.mismatched_match_ids == [mismatched.match_id]


def test_filter_comparable_none_currency_not_flagged_mismatch() -> None:
    row = _row("100", currency=None)
    split = filter_comparable(CUR, [row])
    assert split.included_prices == []
    assert split.mismatched_match_ids == []


# --- Empty comparable set: no div-by-zero -----------------------------------


def test_analyze_empty_comparable_set_is_no_competitor_data_no_div_by_zero() -> None:
    outcome = analyze(Decimal("100"), CUR, [])
    assert outcome.type is AlertType.NO_COMPETITOR_DATA
    assert outcome.comparable_count == 0
    assert outcome.cheapest is None
    assert outcome.average is None
    assert outcome.highest is None
    assert outcome.benchmark_price is None
    assert outcome.discount_vs_average is None


def test_analyze_null_client_price_defensive_step0_no_raise() -> None:
    """analyze() with a None client price (defensive) must not raise (U1)."""
    outcome = analyze(None, CUR, [_row("100")])
    assert outcome.type is AlertType.NO_COMPETITOR_DATA
    assert outcome.severity is AlertSeverity.LOW


# --- NaN / Infinity / over-scale rejection ----------------------------------


def test_decide_rejects_nan_client_price() -> None:
    with pytest.raises(ValueError):
        decide(Decimal("NaN"), Decimal("90"), Decimal("95"), Decimal("100"), 3)


def test_decide_rejects_infinite_client_price() -> None:
    with pytest.raises(ValueError):
        decide(Decimal("Infinity"), Decimal("90"), Decimal("95"), Decimal("100"), 3)


def test_decide_rejects_over_scale_client_price() -> None:
    with pytest.raises(ValueError):
        decide(Decimal("100.12345"), Decimal("90"), Decimal("95"), Decimal("100"), 3)


def test_decide_rejects_nan_cheapest() -> None:
    with pytest.raises(ValueError):
        decide(Decimal("95"), Decimal("NaN"), Decimal("95"), Decimal("100"), 3)


def test_analyze_rejects_nan_competitor_price_included() -> None:
    with pytest.raises(ValueError):
        analyze(Decimal("100"), CUR, [_row("NaN")])


def test_analyze_rejects_infinite_competitor_price_included() -> None:
    with pytest.raises(ValueError):
        analyze(Decimal("100"), CUR, [_row("Infinity")])


def test_analyze_rejects_over_scale_competitor_price_included() -> None:
    with pytest.raises(ValueError):
        analyze(Decimal("100"), CUR, [_row("100.12345")])


# --- Severity map: total + exact over all six types -------------------------


@pytest.mark.parametrize(
    "alert_type, expected_severity",
    [
        (AlertType.NO_COMPETITOR_DATA, AlertSeverity.LOW),
        (AlertType.RISK, AlertSeverity.CRITICAL),
        (AlertType.HIGH_PRICE, AlertSeverity.HIGH),
        (AlertType.CHANCE_TO_INCREASE_PRICE, AlertSeverity.MEDIUM),
        (AlertType.NORMAL, AlertSeverity.NONE),
        (AlertType.CLOSE_TO_COMPETITORS, AlertSeverity.MEDIUM),
    ],
)
def test_severity_map_total_and_exact(alert_type, expected_severity) -> None:
    assert severity_for(alert_type) == expected_severity


def test_severity_map_covers_every_alert_type() -> None:
    assert set(SEVERITY_BY_TYPE.keys()) == set(AlertType)


# --- analyze(): benchmark selection + determinism ---------------------------


def test_analyze_benchmark_price_is_highest_for_risk() -> None:
    outcome = analyze(Decimal("150"), CUR, [_row("90"), _row("100"), _row("110")])
    assert outcome.type is AlertType.RISK
    assert outcome.benchmark_price == Decimal("110")


def test_analyze_benchmark_price_is_cheapest_for_high_price() -> None:
    outcome = analyze(Decimal("95"), CUR, [_row("90"), _row("100"), _row("110")])
    assert outcome.type is AlertType.HIGH_PRICE
    assert outcome.benchmark_price == Decimal("90")


def test_analyze_benchmark_price_is_average_for_discount_types() -> None:
    outcome = analyze(Decimal("97"), CUR, [_row("97"), _row("100"), _row("103")])
    assert outcome.type is AlertType.NORMAL
    assert outcome.benchmark_price == outcome.average


def test_analyze_benchmark_price_none_for_no_competitor_data() -> None:
    outcome = analyze(Decimal("100"), CUR, [])
    assert outcome.benchmark_price is None


def test_analyze_is_byte_identical_across_two_identical_runs() -> None:
    rows = [_row("90", match_id=uuid.UUID(int=1)), _row("110", match_id=uuid.UUID(int=2))]
    first = analyze(Decimal("100"), CUR, rows)
    second = analyze(Decimal("100"), CUR, rows)
    assert first == second
    assert isinstance(first, AlertOutcome)


def test_analyze_mismatched_ids_surfaced_and_excluded_from_benchmarks() -> None:
    mismatched = _row("500", currency="USD")
    matching = [_row("90"), _row("100"), _row("110")]
    outcome = analyze(Decimal("95"), CUR, [mismatched, *matching])
    assert outcome.comparable_count == 3
    assert outcome.mismatched_match_ids == [mismatched.match_id]
    assert outcome.highest == Decimal("110")
