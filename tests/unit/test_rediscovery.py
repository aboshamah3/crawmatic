"""Unit tests for `app_shared.strategy.rediscovery` (T029, US4, FR-020/FR-020a/
FR-020b, `contracts/rediscovery.md`).

Pure, DB-independent boundary tests for `evaluate_rediscovery`: each of the
8 FR-020 conditions firing `trigger=True` in isolation (conditions 1-2 via
`CombinedStats`; conditions 3,4,5,6,7,8 via a `RecentSignals` fixture,
including the FR-020b concrete detection rules for "unrealistic price" and
"template changed"), plus a fully healthy signal set staying
`trigger=False` and a "reset on qualifying success" consecutive-count
boundary. `build_recent_signals`/`apply_rediscovery` touch the DB and are
exercised by the (separately authored, later-phase) skip-clean integration
suite -- not here.
"""

from __future__ import annotations

import functools
from decimal import Decimal

from app_shared.enums import ScrapeErrorCode
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RecentAttemptSignal,
    RecentSignals,
    RediscoveryThresholds,
    evaluate_rediscovery,
)

#: This whole file pins `scope="url_pattern"` (SPEC-domain-profile-scope,
#: 2026-07-11) -- it's the exact v1 pattern-comparison regression suite for
#: condition 8, and its `_profile()` fixture never sets `.domain`, which the
#: `scope="domain"` (new default) branch of condition 8 requires. Domain-scope
#: behavior gets its own fixture in `test_condition_8_domain_scope_*` below.
_evaluate_up = functools.partial(evaluate_rediscovery, scope="url_pattern")

_THRESHOLDS = RediscoveryThresholds(
    consecutive_failures=3,
    success_rate_floor=Decimal("0.80"),
    low_confidence=Decimal("0.75"),
    consecutive_occurrence=3,
)

#: A profile stopped at `example.com/products/*` -- the T014
#: `test_url_pattern_grouping.py` precedent pattern, so condition 8's
#: re-derivation has a known-good vs. known-different URL to compare.
_PROFILE_URL_PATTERN = "example.com/products/*"
_HEALTHY_URL = "https://www.example.com/products/red-shoe-123"
_TEMPLATE_CHANGED_URL = "https://example.com/categories/shoes"


def _profile() -> DomainStrategyProfile:
    return DomainStrategyProfile(url_pattern=_PROFILE_URL_PATTERN)


def _healthy_combined_stats() -> CombinedStats:
    return CombinedStats(
        recent_failure_count=0,
        success_rate=Decimal("0.95"),
        avg_confidence=Decimal("0.90"),
    )


def _healthy_attempt() -> RecentAttemptSignal:
    return RecentAttemptSignal(
        error_code=None,
        status_code=200,
        price=Decimal("19.99"),
        currency_present=True,
        confidence=Decimal("0.90"),
        url=_HEALTHY_URL,
        price_unrealistic=False,
    )


def _healthy_recent_signals(n: int = 5) -> RecentSignals:
    return RecentSignals(attempts=tuple(_healthy_attempt() for _ in range(n)))


def test_healthy_signals_never_trigger() -> None:
    # US4 "healthy signals do not trigger it": rate >= floor, no consecutive
    # failures, confidence >= threshold.
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), _healthy_recent_signals(), _THRESHOLDS
    )
    assert decision.trigger is False
    assert decision.reason


def test_condition_1_recent_failure_count_at_threshold_triggers() -> None:
    combined = CombinedStats(recent_failure_count=3, success_rate=Decimal("0.95"))
    decision = _evaluate_up(_profile(), combined, _healthy_recent_signals(), _THRESHOLDS)
    assert decision.trigger is True
    assert "recent_failure_count" in decision.reason


def test_condition_1_one_below_threshold_does_not_trigger() -> None:
    combined = CombinedStats(recent_failure_count=2, success_rate=Decimal("0.95"))
    decision = _evaluate_up(_profile(), combined, _healthy_recent_signals(), _THRESHOLDS)
    assert decision.trigger is False


def test_condition_2_success_rate_below_floor_triggers() -> None:
    combined = CombinedStats(recent_failure_count=0, success_rate=Decimal("0.79"))
    decision = _evaluate_up(_profile(), combined, _healthy_recent_signals(), _THRESHOLDS)
    assert decision.trigger is True
    assert "success_rate" in decision.reason


def test_condition_2_success_rate_at_floor_does_not_trigger() -> None:
    # The floor itself (0.80) is healthy -- only strictly below triggers.
    combined = CombinedStats(recent_failure_count=0, success_rate=Decimal("0.80"))
    decision = _evaluate_up(_profile(), combined, _healthy_recent_signals(), _THRESHOLDS)
    assert decision.trigger is False


def test_condition_3_empty_selector_consecutive_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=ScrapeErrorCode.PRICE_NOT_FOUND,
            status_code=200,
            price=None,
            currency_present=True,
            confidence=None,
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "empty_selector" in decision.reason


def test_condition_3_selector_broken_also_counts() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=ScrapeErrorCode.SELECTOR_BROKEN,
            status_code=200,
            price=None,
            currency_present=True,
            confidence=None,
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True


def test_condition_4_low_confidence_consecutive_via_recent_signals_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=True,
            confidence=Decimal("0.50"),
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "low_confidence" in decision.reason


def test_condition_4_low_avg_confidence_via_combined_stats_triggers() -> None:
    # FR-020a(b): condition 4 "may use recent_signals confidence and/or
    # combined avg_confidence" -- exercise the combined-stats fallback with
    # otherwise-healthy recent signals.
    combined = CombinedStats(
        recent_failure_count=0, success_rate=Decimal("0.95"), avg_confidence=Decimal("0.50")
    )
    decision = _evaluate_up(_profile(), combined, _healthy_recent_signals(), _THRESHOLDS)
    assert decision.trigger is True
    assert "avg_confidence" in decision.reason


def test_condition_5_repeated_403_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=ScrapeErrorCode.HTTP_403,
            status_code=403,
            price=None,
            currency_present=True,
            confidence=None,
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "403_429" in decision.reason


def test_condition_5_repeated_429_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=ScrapeErrorCode.HTTP_429,
            status_code=429,
            price=None,
            currency_present=True,
            confidence=None,
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True


def test_condition_6_currency_absent_consecutive_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=False,
            confidence=Decimal("0.90"),
            url=_HEALTHY_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "currency_absent" in decision.reason


def test_condition_7_unrealistic_price_consecutive_triggers() -> None:
    # FR-020b: "unrealistic" = fails the §18 price-validation bounds --
    # `price_unrealistic` is the precomputed signal `build_recent_signals`
    # derives from that bounds re-check (not re-validated here, pure
    # evaluator trusts the flag it's handed).
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("999999.99"),
            currency_present=True,
            confidence=Decimal("0.90"),
            url=_HEALTHY_URL,
            price_unrealistic=True,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "unrealistic_price" in decision.reason


def test_condition_8_template_changed_consecutive_triggers() -> None:
    # FR-020b: "template changed" = the re-derived url_pattern (current
    # URL_PATTERN_ALGORITHM_VERSION, via the shipped `derive_url_pattern`)
    # of recently observed URLs no longer equals the profile's stored
    # `url_pattern`, for >= the consecutive threshold.
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=True,
            confidence=Decimal("0.90"),
            url=_TEMPLATE_CHANGED_URL,
        )
        for _ in range(3)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
    assert "template_changed" in decision.reason


def test_reset_on_qualifying_success_breaks_the_consecutive_streak() -> None:
    # A healthy attempt in between two shorter failure runs resets the
    # count -- neither run alone reaches the threshold of 3, so this must
    # NOT trigger (FR-020 "reset on a qualifying success").
    failing = RecentAttemptSignal(
        error_code=ScrapeErrorCode.PRICE_NOT_FOUND,
        status_code=200,
        price=None,
        currency_present=True,
        confidence=None,
        url=_HEALTHY_URL,
    )
    attempts = (failing, failing, _healthy_attempt(), failing, failing, failing)
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is False


def test_below_consecutive_occurrence_threshold_does_not_trigger() -> None:
    # Only 2 consecutive empty-selector outcomes -- below the default
    # threshold of 3.
    attempts = tuple(
        RecentAttemptSignal(
            error_code=ScrapeErrorCode.PRICE_NOT_FOUND,
            status_code=200,
            price=None,
            currency_present=True,
            confidence=None,
            url=_HEALTHY_URL,
        )
        for _ in range(2)
    )
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is False


def test_empty_recent_signals_with_healthy_combined_stats_does_not_trigger() -> None:
    decision = _evaluate_up(
        _profile(), _healthy_combined_stats(), RecentSignals(attempts=()), _THRESHOLDS
    )
    assert decision.trigger is False


# --- scope="domain" (default, SPEC-domain-profile-scope, 2026-07-11) -------
# Condition 8 compares the observed URL's host to `profile.domain` instead
# of re-deriving a v1 pattern -- it should never fire on same-domain URLs,
# and should fire when the observed host genuinely differs.

_DOMAIN_PROFILE_DOMAIN = "example.com"


def _domain_profile() -> DomainStrategyProfile:
    return DomainStrategyProfile(domain=_DOMAIN_PROFILE_DOMAIN, url_pattern=_DOMAIN_PROFILE_DOMAIN)


def test_condition_8_domain_scope_same_domain_never_triggers() -> None:
    # Under scope="domain", a different URL *pattern* on the SAME host
    # (what would have tripped v1's derive_url_pattern comparison) must not
    # trigger -- the whole point of the domain-scope fix.
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=True,
            confidence=Decimal("0.90"),
            url=_TEMPLATE_CHANGED_URL,  # https://example.com/categories/shoes
        )
        for _ in range(3)
    )
    decision = evaluate_rediscovery(
        _domain_profile(),
        _healthy_combined_stats(),
        RecentSignals(attempts=attempts),
        _THRESHOLDS,
        scope="domain",
    )
    assert decision.trigger is False


def test_condition_8_domain_scope_different_host_triggers() -> None:
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=True,
            confidence=Decimal("0.90"),
            url="https://other-domain.com/products/red-shoe-123",
        )
        for _ in range(3)
    )
    decision = evaluate_rediscovery(
        _domain_profile(),
        _healthy_combined_stats(),
        RecentSignals(attempts=attempts),
        _THRESHOLDS,
        scope="domain",
    )
    assert decision.trigger is True
    assert "template_changed" in decision.reason


def test_condition_8_domain_scope_default_matches_explicit_domain_kwarg() -> None:
    # `scope` defaults to "domain" (Settings.STRATEGY_PROFILE_SCOPE's default)
    # -- omitting it entirely must behave identically to passing it explicitly.
    attempts = tuple(
        RecentAttemptSignal(
            error_code=None,
            status_code=200,
            price=Decimal("19.99"),
            currency_present=True,
            confidence=Decimal("0.90"),
            url="https://other-domain.com/products/red-shoe-123",
        )
        for _ in range(3)
    )
    decision = evaluate_rediscovery(
        _domain_profile(), _healthy_combined_stats(), RecentSignals(attempts=attempts), _THRESHOLDS
    )
    assert decision.trigger is True
