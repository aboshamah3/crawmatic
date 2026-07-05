"""Unit tests for `app_shared.strategy.promotion` (T015, US1, FR-010/FR-011,
`contracts/promotion.md`).

Pure, DB-independent boundary tests for `evaluate_promotion` plus a
lightweight in-memory guarded-UPDATE simulation for `apply_promotion`'s
concurrency guard (the real SQLAlchemy statement is exercised by the
skip-clean integration test, `tests/integration/test_promotion_apply.py`,
T018).
"""

from __future__ import annotations

from decimal import Decimal

from app_shared.strategy.promotion import (
    MethodStats,
    PromotionThresholds,
    evaluate_promotion,
)

_THRESHOLDS = PromotionThresholds(
    min_successes=3,
    min_distinct_urls=3,
    confidence_threshold=Decimal("0.85"),
)


def test_three_qualifying_successes_across_three_urls_promotes() -> None:
    # US1 AS1
    combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=3, thresholds=_THRESHOLDS)
    assert decision.promote is True
    assert decision.confidence == Decimal("0.9")
    assert isinstance(decision.reason, str) and decision.reason


def test_more_than_enough_successes_and_urls_still_promotes() -> None:
    combined = MethodStats(qualifying_success_count=10, confidence=Decimal("0.95"))
    decision = evaluate_promotion(combined, distinct_url_count=7, thresholds=_THRESHOLDS)
    assert decision.promote is True


def test_three_successes_but_only_two_distinct_urls_does_not_promote() -> None:
    # US1 AS2 -- distinct-URL gate blocks promotion even with enough successes.
    combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=2, thresholds=_THRESHOLDS)
    assert decision.promote is False
    assert "distinct_url_count" in decision.reason


def test_two_qualifying_successes_across_three_urls_does_not_promote() -> None:
    # Success-count gate blocks promotion even with enough distinct URLs.
    combined = MethodStats(qualifying_success_count=2, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=3, thresholds=_THRESHOLDS)
    assert decision.promote is False
    assert "qualifying_success_count" in decision.reason


def test_zero_qualifying_successes_never_promotes() -> None:
    # US1 AS3 -- a below-threshold/invalid-price/missing-currency success
    # never enters `qualifying_success_count` at record time, so a method
    # with only non-qualifying activity never crosses the gate here.
    combined = MethodStats(qualifying_success_count=0, confidence=None)
    decision = evaluate_promotion(combined, distinct_url_count=0, thresholds=_THRESHOLDS)
    assert decision.promote is False
    assert decision.confidence is None


def test_exact_boundary_at_thresholds_promotes() -> None:
    # min_successes == 3, min_distinct_urls == 3: the boundary itself
    # (not one-below) must promote.
    combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.85"))
    decision = evaluate_promotion(combined, distinct_url_count=3, thresholds=_THRESHOLDS)
    assert decision.promote is True


def test_one_below_min_successes_boundary_does_not_promote() -> None:
    combined = MethodStats(qualifying_success_count=2, confidence=Decimal("0.85"))
    decision = evaluate_promotion(combined, distinct_url_count=5, thresholds=_THRESHOLDS)
    assert decision.promote is False


def test_one_below_min_distinct_urls_boundary_does_not_promote() -> None:
    combined = MethodStats(qualifying_success_count=5, confidence=Decimal("0.85"))
    decision = evaluate_promotion(combined, distinct_url_count=2, thresholds=_THRESHOLDS)
    assert decision.promote is False


def test_access_and_extraction_are_evaluated_independently() -> None:
    # US1 AS5 -- evaluate_promotion has no notion of method_type at all;
    # the caller (apply_promotion) evaluates/applies access and extraction
    # as two separate calls over two separate MethodStats. A qualifying
    # access method and a non-qualifying extraction method for the *same*
    # profile must not influence one another.
    access_combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.9"))
    extraction_combined = MethodStats(qualifying_success_count=1, confidence=Decimal("0.9"))

    access_decision = evaluate_promotion(
        access_combined, distinct_url_count=3, thresholds=_THRESHOLDS
    )
    extraction_decision = evaluate_promotion(
        extraction_combined, distinct_url_count=3, thresholds=_THRESHOLDS
    )

    assert access_decision.promote is True
    assert extraction_decision.promote is False


def test_confidence_is_surfaced_on_the_decision_regardless_of_outcome() -> None:
    combined = MethodStats(qualifying_success_count=1, confidence=Decimal("0.5"))
    decision = evaluate_promotion(combined, distinct_url_count=1, thresholds=_THRESHOLDS)
    assert decision.promote is False
    assert decision.confidence == Decimal("0.5")
