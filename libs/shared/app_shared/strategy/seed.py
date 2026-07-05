"""Discovery-seed helper (`contracts/discovery.md` "Profile seeding" +
"Payload", FR-018/FR-019, D9, US3).

Two small, deliberately pure pieces shared by **both** discovery trigger
paths (the automatic enqueue from US2 `resolve_or_create_strategy_profile`
and the operator `POST /v1/strategy/discovery-runs`, spec Clarification
#3) and by rediscovery re-runs (`contracts/rediscovery.md`) against an
existing `DEGRADED`/`LEARNING` profile:

* :func:`validate_sample_size` — the `3..10` bound (FR-019, US3 AS2),
  shared by the worker task (`apps/workers/app/workers/tasks_strategy.py`)
  and the operator API schema (`apps/api/app/schemas/strategy.py`) so
  both reject out-of-bounds samples identically instead of drifting.
* :func:`seed_from_discovery` — mutates an already-resolved
  :class:`~app_shared.models.strategy.DomainStrategyProfile` in place per
  the discovery outcome and returns it. Pure/no I/O (stdlib +
  `decimal.Decimal` + `evaluate_promotion` only) — the same
  no-session/no-DB testability as `app_shared.strategy.resolution
  .resolve_strategy_start` (T019's `test_resolution.py` precedent).
  Get-or-create of the `profile` argument itself is the **caller's**
  responsibility (the worker task resolves/creates it via
  `app_shared.strategy.repository.resolve_profile` plus a plain insert on
  a miss — deliberately NOT `resolve_or_create_strategy_profile`, which
  also enqueues automatic discovery and would loop back on itself here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app_shared.enums import AccessMethod, ExtractionMethod, StrategyStatus
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.promotion import MethodStats, PromotionThresholds, evaluate_promotion

__all__ = ["DiscoverySeedConfidences", "seed_from_discovery", "validate_sample_size"]


def validate_sample_size(sample_size: int, *, min_sample: int, max_sample: int) -> bool:
    """`True` iff `min_sample <= sample_size <= max_sample` (FR-019, US3 AS2).

    Pure boundary check, no `Settings` import here so it stays trivially
    unit-testable with explicit bounds — callers pass
    `Settings.STRATEGY_DISCOVERY_MIN_SAMPLE`/`_MAX_SAMPLE`.
    """
    return min_sample <= sample_size <= max_sample


@dataclass(frozen=True)
class DiscoverySeedConfidences:
    """Per-method confidence + qualifying-sample counts from a discovery probe
    (contracts/discovery.md "Profile seeding"). Feeds `evaluate_promotion`
    (reused, never re-derived) to decide whether the seeded profile already
    satisfies the 3-confirmation rule (`-> ACTIVE`) or not yet (`-> LEARNING`).

    Access and extraction are tracked separately (US1 AS5 precedent) even
    though a discovery probe's qualifying observations always used the
    *same* winning `(access, extraction)` pair together -- a caller that
    only has one combined count may pass the same values for both halves.
    """

    access_confidence: Decimal | None
    access_qualifying_count: int
    access_distinct_url_count: int
    extraction_confidence: Decimal | None
    extraction_qualifying_count: int
    extraction_distinct_url_count: int


def seed_from_discovery(
    profile: DomainStrategyProfile,
    *,
    winning_access: AccessMethod | None,
    winning_extraction: ExtractionMethod | None,
    confidences: DiscoverySeedConfidences | None,
    thresholds: PromotionThresholds,
) -> DomainStrategyProfile:
    """Mutate `profile` per one discovery run's outcome; return it (US3 AS1/AS3/AS4).

    `winning_access is None` (`NO_WINNER`, US3 AS4): the profile is left
    **untouched** -- it stays `DISCOVERY_REQUIRED` (or whatever status a
    rediscovery re-run against an existing `DEGRADED`/`LEARNING` profile
    already carried, contracts/discovery.md "Profile seeding").

    Otherwise (`COMPLETED`, US3 AS1/AS3): sets `preferred_access_method`/
    `access_confidence` and (when `winning_extraction` is not `None`)
    `preferred_extraction_method`/`extraction_confidence`, stamps
    `last_discovery_at`, and reuses `evaluate_promotion` per method to
    decide `-> ACTIVE` (the sample already satisfies the 3-confirmation
    rule for every winning method) or `-> LEARNING` (winner seeded,
    awaiting live confirmation).
    """
    if winning_access is None:
        return profile

    if confidences is None:
        raise ValueError("confidences is required when winning_access is not None")

    profile.preferred_access_method = winning_access
    profile.access_confidence = confidences.access_confidence
    profile.last_discovery_at = datetime.now(timezone.utc)

    access_decision = evaluate_promotion(
        MethodStats(
            qualifying_success_count=confidences.access_qualifying_count,
            confidence=confidences.access_confidence,
        ),
        confidences.access_distinct_url_count,
        thresholds,
    )
    fully_confirmed = access_decision.promote

    if winning_extraction is not None:
        profile.preferred_extraction_method = winning_extraction
        profile.extraction_confidence = confidences.extraction_confidence

        extraction_decision = evaluate_promotion(
            MethodStats(
                qualifying_success_count=confidences.extraction_qualifying_count,
                confidence=confidences.extraction_confidence,
            ),
            confidences.extraction_distinct_url_count,
            thresholds,
        )
        fully_confirmed = fully_confirmed and extraction_decision.promote

    profile.status = StrategyStatus.ACTIVE if fully_confirmed else StrategyStatus.LEARNING
    return profile
