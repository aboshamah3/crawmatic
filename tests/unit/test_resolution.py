"""Unit tests for `app_shared.strategy.resolution.resolve_strategy_start`
(T019, US2, FR-013/FR-014/FR-015, `contracts/consumption.md`).

Pure, DB-independent boundary tests: `DomainStrategyProfile` instances are
constructed directly (no session/engine, no DB) since `resolve_strategy_start`
only ever reads plain attributes off whatever is handed to it.
"""

from __future__ import annotations

from app_shared.enums import AccessMethod, ExtractionMethod, StrategyStatus
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.resolution import StrategyStart, resolve_strategy_start

_ALGORITHM_VERSION = 1


def _profile(**overrides: object) -> DomainStrategyProfile:
    defaults: dict[str, object] = dict(
        status=StrategyStatus.ACTIVE,
        preferred_access_method=AccessMethod.PROXY_HTTP,
        preferred_extraction_method=ExtractionMethod.CSS,
        url_pattern_version=_ALGORITHM_VERSION,
    )
    defaults.update(overrides)
    return DomainStrategyProfile(**defaults)


def test_active_profile_returns_preferred_pair() -> None:
    # US2 AS1
    profile = _profile(status=StrategyStatus.ACTIVE)
    result = resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION)
    assert result == StrategyStart(
        access_method=AccessMethod.PROXY_HTTP, extraction_method=ExtractionMethod.CSS
    )


def test_learning_with_preference_returns_preferred_pair() -> None:
    # US2 AS1 -- LEARNING is eligible once a preferred method is set.
    profile = _profile(status=StrategyStatus.LEARNING)
    result = resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION)
    assert result == StrategyStart(
        access_method=AccessMethod.PROXY_HTTP, extraction_method=ExtractionMethod.CSS
    )


def test_learning_without_preference_returns_none() -> None:
    profile = _profile(
        status=StrategyStatus.LEARNING,
        preferred_access_method=None,
        preferred_extraction_method=None,
    )
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_missing_profile_returns_none() -> None:
    # US2 AS2
    assert resolve_strategy_start(None, algorithm_version=_ALGORITHM_VERSION) is None


def test_disabled_profile_returns_none() -> None:
    # US2 AS3
    profile = _profile(status=StrategyStatus.DISABLED)
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_degraded_without_preference_returns_none() -> None:
    profile = _profile(
        status=StrategyStatus.DEGRADED,
        preferred_access_method=None,
        preferred_extraction_method=None,
    )
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_degraded_with_preference_still_returns_none() -> None:
    # DEGRADED is never eligible outright (only ACTIVE, or LEARNING-with-
    # preference, are) -- a stale preferred method on a DEGRADED row must
    # not be resumed (FR-014, US2 AS3).
    profile = _profile(status=StrategyStatus.DEGRADED)
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_discovery_required_returns_none() -> None:
    profile = _profile(status=StrategyStatus.DISCOVERY_REQUIRED)
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_url_pattern_version_mismatch_returns_none() -> None:
    # US2 AS4 -- a mismatched-version pattern is never used, even if the
    # profile would otherwise be eligible.
    profile = _profile(status=StrategyStatus.ACTIVE, url_pattern_version=2)
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_active_with_no_preferred_access_method_returns_none() -> None:
    # Defensive: an ACTIVE profile with no preferred access method should
    # not happen in practice (promotion always sets it before flipping to
    # ACTIVE), but the resolver must not crash / must not fabricate a
    # start from a None access method.
    profile = _profile(
        status=StrategyStatus.ACTIVE,
        preferred_access_method=None,
        preferred_extraction_method=None,
    )
    assert resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION) is None


def test_extraction_method_may_be_none_while_access_is_set() -> None:
    # Access and extraction are learned/promoted independently (US1 AS5) --
    # a confirmed access method with no extraction preference yet is a
    # valid StrategyStart.
    profile = _profile(status=StrategyStatus.ACTIVE, preferred_extraction_method=None)
    result = resolve_strategy_start(profile, algorithm_version=_ALGORITHM_VERSION)
    assert result == StrategyStart(access_method=AccessMethod.PROXY_HTTP, extraction_method=None)
