"""Promotion: pure evaluator + guarded apply (`contracts/promotion.md`, D9, FR-010/FR-011, US1).

Two halves, deliberately separated (mirrors `app_shared/access/engine.py`'s
pure-evaluator shape):

* :func:`evaluate_promotion` — a **pure**, framework-agnostic function.
  stdlib + `decimal.Decimal` only, no SQLAlchemy/Redis/FastAPI/Scrapy
  imports (grep-enforced by the caller's verification step, T042). Takes
  already-combined (persisted + pending) counts and decides whether a
  method has earned promotion.
* :func:`apply_promotion` — the guarded, atomic apply. Used by the flush
  task (US5, T035) once per qualifying method (access/extraction
  evaluated **separately**, FR-011). A single `UPDATE ... WHERE id=:pid
  AND status IN (...) AND (preferred_* IS NULL OR preferred_* <> :m)`
  statement so two workers flushing the same profile concurrently cannot
  double-promote or corrupt `confirmed_success_count` (Edge Cases
  "Concurrent promotion").

## Qualifying success (gated by the caller at record time)

A success counts toward `combined.qualifying_success_count` only if,
at record time (`app_shared/strategy/stats_buffer.py`, US5):
confidence >= `STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD`, price is a
valid numeric `Decimal` (`app_shared.money`), and currency is valid
when required. This module does not re-check that gate — it trusts
`combined.qualifying_success_count`/`distinct_url_count` already reflect
only qualifying successes (contracts/promotion.md).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.orm import Session

from app_shared.enums import MethodType, StrategyStatus, validate_method_name
from app_shared.models.strategy import DomainStrategyProfile

logger = logging.getLogger(__name__)

#: Statuses from which a qualifying method may still promote a profile to
#: ACTIVE (contracts/promotion.md "Concurrent promotion"). DISABLED is
#: excluded -- an operator-disabled profile never auto-promotes (FR-014).
_PROMOTABLE_STATUSES: tuple[StrategyStatus, ...] = (
    StrategyStatus.DISCOVERY_REQUIRED,
    StrategyStatus.LEARNING,
    StrategyStatus.DEGRADED,
)


@dataclass(frozen=True)
class MethodStats:
    """Combined (persisted + pending) counts for one
    `(profile, method_type, method_name)` key -- the input `evaluate_promotion`
    reasons over.

    `qualifying_success_count` is tracked as its own counter (never the
    raw `success_count`, which also includes non-qualifying successes
    kept only for `success_rate`) -- contracts/promotion.md "Note".
    `confidence` is the method's running average confidence (persisted
    `avg_confidence` combined with any pending delta), surfaced on the
    decision so the caller can set `access_confidence`/
    `extraction_confidence` on promotion.
    """

    qualifying_success_count: int
    confidence: Decimal | None


@dataclass(frozen=True)
class PromotionThresholds:
    """Promotion boundary knobs (`Settings.STRATEGY_PROMOTION_*`, data-model §7).

    `confidence_threshold` is bundled here for a single source of truth
    shared with the record-time qualifying gate (US5 `stats_buffer.py`)
    even though `evaluate_promotion` itself only compares
    `qualifying_success_count`/`distinct_url_count` -- the confidence bar
    was already enforced before a success ever entered those counts.
    """

    min_successes: int
    min_distinct_urls: int
    confidence_threshold: Decimal


@dataclass(frozen=True)
class PromotionDecision:
    """Outcome of :func:`evaluate_promotion` for one method (`contracts/promotion.md`)."""

    promote: bool
    confidence: Decimal | None
    reason: str


def evaluate_promotion(
    combined: MethodStats,
    distinct_url_count: int,
    thresholds: PromotionThresholds,
) -> PromotionDecision:
    """`promote = qualifying_success_count >= min_successes AND distinct_url_count >= min_distinct_urls`.

    Deterministic, framework-agnostic (contracts/promotion.md, D9):

    - US1 AS1: 3 qualifying successes across >=3 distinct URLs -> `promote=True`.
    - US1 AS2: 3 successes but only 2 distinct URLs -> `promote=False` (the
      distinct-URL SET is only ever populated by qualifying successes, so
      this gate cannot be satisfied by volume alone).
    - US1 AS3: a below-threshold/invalid-price/missing-required-currency
      success never entered `qualifying_success_count` in the first place
      -- enforced by the caller at record time, not here.
    """
    if combined.qualifying_success_count < thresholds.min_successes:
        return PromotionDecision(
            promote=False,
            confidence=combined.confidence,
            reason=(
                f"qualifying_success_count={combined.qualifying_success_count} < "
                f"min_successes={thresholds.min_successes}"
            ),
        )

    if distinct_url_count < thresholds.min_distinct_urls:
        return PromotionDecision(
            promote=False,
            confidence=combined.confidence,
            reason=(
                f"distinct_url_count={distinct_url_count} < "
                f"min_distinct_urls={thresholds.min_distinct_urls}"
            ),
        )

    return PromotionDecision(
        promote=True,
        confidence=combined.confidence,
        reason=(
            f"qualifying_success_count={combined.qualifying_success_count} >= "
            f"min_successes={thresholds.min_successes} AND "
            f"distinct_url_count={distinct_url_count} >= "
            f"min_distinct_urls={thresholds.min_distinct_urls}"
        ),
    )


def apply_promotion(
    session: Session,
    profile_id: uuid.UUID | str,
    *,
    method_type: MethodType,
    method_name: str,
    decision: PromotionDecision,
) -> bool:
    """Guarded atomic apply of one qualifying method's promotion (used by the
    flush task, US5 T035). Access and extraction are applied independently
    (FR-011, US1 AS5) -- call this once per method_type that qualifies.

    No-op (returns `False`, no statement executed) when `decision.promote`
    is `False`. Otherwise issues exactly one atomic
    `UPDATE domain_strategy_profiles SET preferred_{type}_method=:m,
    {type}_confidence=:c, confirmed_success_count=confirmed_success_count+1,
    status='ACTIVE' WHERE id=:pid AND status IN ('DISCOVERY_REQUIRED',
    'LEARNING','DEGRADED') AND (preferred_{type}_method IS NULL OR
    preferred_{type}_method <> :m)` (contracts/promotion.md "Apply").

    Returns `True` iff this call's UPDATE actually changed a row (this
    call made progress); `False` if a concurrent worker already won the
    race (`rowcount == 0`) or the profile was `DISABLED`/already carries
    a *different* status disqualifying it -- so two workers flushing the
    same profile concurrently cannot double-promote or corrupt
    `confirmed_success_count` (Edge Cases "Concurrent promotion").
    """
    if not decision.promote:
        return False

    validated_name = validate_method_name(method_type, method_name)

    if method_type is MethodType.ACCESS:
        method_column = DomainStrategyProfile.preferred_access_method
        values: dict[str, object] = {
            "preferred_access_method": validated_name,
            "access_confidence": decision.confidence,
            "confirmed_success_count": DomainStrategyProfile.confirmed_success_count + 1,
            "status": StrategyStatus.ACTIVE,
        }
    elif method_type is MethodType.EXTRACTION:
        method_column = DomainStrategyProfile.preferred_extraction_method
        values = {
            "preferred_extraction_method": validated_name,
            "extraction_confidence": decision.confidence,
            "confirmed_success_count": DomainStrategyProfile.confirmed_success_count + 1,
            "status": StrategyStatus.ACTIVE,
        }
    else:  # pragma: no cover - MethodType has exactly two members
        raise ValueError(f"unknown method_type: {method_type!r}")

    stmt = (
        update(DomainStrategyProfile)
        .where(
            DomainStrategyProfile.id == profile_id,
            DomainStrategyProfile.status.in_(_PROMOTABLE_STATUSES),
            (method_column.is_(None)) | (method_column != validated_name),
        )
        .values(**values)
    )
    result = session.execute(stmt)
    promoted = bool(result.rowcount and result.rowcount > 0)
    if promoted:
        # Structured observability event (contracts/api-and-observability.md
        # §Observability, T040): a method was actually promoted this call.
        logger.info(
            "app_shared.strategy.promotion: strategy_method_promoted "
            "profile_id=%s method_type=%s method_name=%s confidence=%s",
            profile_id,
            method_type.value,
            validated_name,
            decision.confidence,
        )
    return promoted
