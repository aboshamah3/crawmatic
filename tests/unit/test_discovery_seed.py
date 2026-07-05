"""Unit tests for `app_shared.strategy.seed` (T024, US3, FR-018/FR-019,
`contracts/discovery.md`).

Pure, DB-independent boundary tests: `validate_sample_size` is a plain
int comparison; `seed_from_discovery` mutates a directly-constructed
`DomainStrategyProfile` (no session/engine, no DB) exactly like
`tests/unit/test_resolution.py`'s `resolve_strategy_start` precedent —
`seed_from_discovery` never touches I/O itself, only whatever `profile`
object it is handed.
"""

from __future__ import annotations

from decimal import Decimal

from app_shared.enums import AccessMethod, ExtractionMethod, StrategyStatus
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.promotion import PromotionThresholds
from app_shared.strategy.seed import DiscoverySeedConfidences, seed_from_discovery, validate_sample_size

_THRESHOLDS = PromotionThresholds(
    min_successes=3, min_distinct_urls=3, confidence_threshold=Decimal("0.85")
)


def _profile(**overrides: object) -> DomainStrategyProfile:
    defaults: dict[str, object] = dict(
        status=StrategyStatus.DISCOVERY_REQUIRED,
        url_pattern_version=1,
    )
    defaults.update(overrides)
    return DomainStrategyProfile(**defaults)


# --- validate_sample_size (AS2, FR-019) -------------------------------------


def test_sample_size_two_is_rejected() -> None:
    assert validate_sample_size(2, min_sample=3, max_sample=10) is False


def test_sample_size_eleven_is_rejected() -> None:
    assert validate_sample_size(11, min_sample=3, max_sample=10) is False


def test_sample_sizes_three_through_ten_are_accepted() -> None:
    for size in range(3, 11):
        assert validate_sample_size(size, min_sample=3, max_sample=10) is True


def test_zero_is_rejected() -> None:
    assert validate_sample_size(0, min_sample=3, max_sample=10) is False


# --- seed_from_discovery: NO_WINNER (AS4) -----------------------------------


def test_no_winner_leaves_profile_discovery_required() -> None:
    profile = _profile()
    result = seed_from_discovery(
        profile,
        winning_access=None,
        winning_extraction=None,
        confidences=None,
        thresholds=_THRESHOLDS,
    )
    assert result is profile
    assert profile.status == StrategyStatus.DISCOVERY_REQUIRED
    assert profile.preferred_access_method is None
    assert profile.preferred_extraction_method is None


def test_no_winner_leaves_a_degraded_profile_untouched() -> None:
    # A rediscovery re-run against an existing DEGRADED profile that
    # still comes back NO_WINNER never regresses the profile's status.
    profile = _profile(status=StrategyStatus.DEGRADED)
    seed_from_discovery(
        profile,
        winning_access=None,
        winning_extraction=None,
        confidences=None,
        thresholds=_THRESHOLDS,
    )
    assert profile.status == StrategyStatus.DEGRADED


# --- seed_from_discovery: winner, already-confirmed -> ACTIVE (AS3) --------


def test_winner_meeting_3_confirmation_rule_moves_to_active() -> None:
    profile = _profile()
    confidences = DiscoverySeedConfidences(
        access_confidence=Decimal("0.9"),
        access_qualifying_count=3,
        access_distinct_url_count=3,
        extraction_confidence=Decimal("0.9"),
        extraction_qualifying_count=3,
        extraction_distinct_url_count=3,
    )

    seed_from_discovery(
        profile,
        winning_access=AccessMethod.PROXY_HTTP,
        winning_extraction=ExtractionMethod.CSS,
        confidences=confidences,
        thresholds=_THRESHOLDS,
    )

    assert profile.status == StrategyStatus.ACTIVE
    assert profile.preferred_access_method == AccessMethod.PROXY_HTTP
    assert profile.preferred_extraction_method == ExtractionMethod.CSS
    assert profile.access_confidence == Decimal("0.9")
    assert profile.extraction_confidence == Decimal("0.9")
    assert profile.last_discovery_at is not None


# --- seed_from_discovery: winner, not yet confirmed -> LEARNING ------------


def test_winner_below_3_confirmation_rule_moves_to_learning() -> None:
    profile = _profile()
    confidences = DiscoverySeedConfidences(
        access_confidence=Decimal("0.9"),
        access_qualifying_count=1,
        access_distinct_url_count=1,
        extraction_confidence=Decimal("0.9"),
        extraction_qualifying_count=1,
        extraction_distinct_url_count=1,
    )

    seed_from_discovery(
        profile,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=ExtractionMethod.JSON_LD,
        confidences=confidences,
        thresholds=_THRESHOLDS,
    )

    assert profile.status == StrategyStatus.LEARNING
    assert profile.preferred_access_method == AccessMethod.DIRECT_HTTP
    assert profile.preferred_extraction_method == ExtractionMethod.JSON_LD


def test_winner_with_no_extraction_method_only_sets_access() -> None:
    # A winning access method with no reliable extraction hit yet -- the
    # extraction half is left unset, and the ACTIVE/LEARNING decision
    # only considers access (mirrors US1 AS5's independent evaluation).
    profile = _profile()
    confidences = DiscoverySeedConfidences(
        access_confidence=Decimal("0.9"),
        access_qualifying_count=3,
        access_distinct_url_count=3,
        extraction_confidence=None,
        extraction_qualifying_count=0,
        extraction_distinct_url_count=0,
    )

    seed_from_discovery(
        profile,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=None,
        confidences=confidences,
        thresholds=_THRESHOLDS,
    )

    assert profile.status == StrategyStatus.ACTIVE
    assert profile.preferred_access_method == AccessMethod.DIRECT_HTTP
    assert profile.preferred_extraction_method is None
    assert profile.extraction_confidence is None


def test_missing_confidences_with_a_winner_raises() -> None:
    import pytest

    profile = _profile()
    with pytest.raises(ValueError):
        seed_from_discovery(
            profile,
            winning_access=AccessMethod.DIRECT_HTTP,
            winning_extraction=ExtractionMethod.JSON_LD,
            confidences=None,
            thresholds=_THRESHOLDS,
        )
