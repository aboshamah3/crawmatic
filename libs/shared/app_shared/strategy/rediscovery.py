"""Rediscovery: pure evaluator + recent-signal builder + guarded apply
(`contracts/rediscovery.md`, D8, FR-020/FR-020a/FR-020b, US4).

Three pieces, mirroring `promotion.py`'s pure-evaluator/apply split:

* :func:`evaluate_rediscovery` — a **pure**, framework-agnostic function
  (stdlib + `decimal.Decimal` + the shipped `app_shared.url_pattern
  .derive_url_pattern` only). Decides *trigger/no-trigger* for one
  profile from two already-assembled signal sources (FR-020a) — never
  touches the DB itself.
* :func:`build_recent_signals` — assembles the per-attempt-outcome
  signal source (:class:`RecentSignals`) from the last-N `request_attempts`
  rows for the profile's preferred access method (joined to their paired
  `price_observations` row and, for the §18 bounds re-check, the match's
  `scrape_profiles.validation_rules`). Blocking (SQLAlchemy reads) — must
  only ever be called off the hot path (the stats-flush task / the
  periodic light re-check, never the reactor/spider — Constitution V).
* :func:`apply_rediscovery` — the guarded, atomic apply: `DEGRADED` +
  `STRATEGY_DISCOVERY_RUN` enqueue + `strategy_rediscovery_triggered`.

## Two signal sources (FR-020a) — no hot-path stats-schema widening

`combined_stats` (:class:`CombinedStats`) carries the **aggregate/counter**
conditions (1: `recent_failure_count`, 2: cumulative `success_rate`) —
persisted `strategy_attempt_stats` plus pending buffered deltas (US5,
FR-024), assembled by the caller. `recent_signals` (:class:`RecentSignals`)
carries the **per-attempt-outcome** conditions (3, 5, 6, 7, 8) — read off
the hot path from `request_attempts`/`price_observations`, never a widened
`stats_buffer.py` recorder (which stays success/failure/rt/confidence/URL
only, FR-022).

## §18 bounds re-check without a `scrape_core` import

Condition 7 ("price values become unrealistic", FR-020b) reuses the same
`app_shared.money.parse_money` boundary primitive
`scrape_core.validation.validate_candidate`'s bounds step (step 4) is
built on, but does **not** import `scrape_core` itself:
`libs/scrape-core/pyproject.toml` declares "app_shared must never depend
on this package" (scrape-core depends on app_shared, not the reverse) —
importing it here would be a circular/layering violation. `_price_fails_bounds`
duplicates just that one small bounds comparison, not the extraction/
candidate machinery (not a rebuild of fetch/extract, tasks.md "Reuse, do
not rebuild").
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeErrorCode, StrategyStatus
from app_shared.messaging import enqueue
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.observations import PriceObservation, RequestAttempt
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.money import parse_money
from app_shared.task_names import STRATEGY_DISCOVERY_RUN
from app_shared.url_pattern import derive_url_pattern

__all__ = [
    "CombinedStats",
    "RecentAttemptSignal",
    "RecentSignals",
    "RediscoveryDecision",
    "RediscoveryThresholds",
    "apply_rediscovery",
    "build_recent_signals",
    "evaluate_rediscovery",
]

logger = logging.getLogger(__name__)

#: `STRATEGY_DISCOVERY_RUN` runs on its own queue, distinct from `maintenance`
#: (data-model.md §8, contracts/discovery.md) — the same constant every
#: other SPEC-12 enqueue site (`resolution.py`, `tasks_strategy.py`) uses.
_DISCOVERY_QUEUE = "strategy_discovery"

#: Condition 3 "selector returns empty" (contracts/rediscovery.md row 3).
_EMPTY_SELECTOR_CODES: tuple[ScrapeErrorCode, ...] = (
    ScrapeErrorCode.PRICE_NOT_FOUND,
    ScrapeErrorCode.SELECTOR_BROKEN,
)

#: Condition 5 "repeated 403/429" (contracts/rediscovery.md row 5).
_BLOCKED_CODES: tuple[ScrapeErrorCode, ...] = (
    ScrapeErrorCode.HTTP_403,
    ScrapeErrorCode.HTTP_429,
)

#: Default last-N window `build_recent_signals` reads — comfortably above
#: the default consecutive-occurrence threshold (3) so a full streak (or
#: its interruption by a qualifying success) is always observable. A local
#: implementation constant, not a `Settings` knob (data-model §7's 10
#: SPEC-12 knobs are exhaustive, T004) — the `_PROBE_TIMEOUT_SECONDS`
#: precedent in `tasks_strategy.py`.
_DEFAULT_RECENT_SIGNAL_LIMIT = 10

#: Statuses a rediscovery trigger may transition out of. Only `ACTIVE` —
#: `DISABLED` is never rediscovered (US4 AS1); `DEGRADED`/`LEARNING`/
#: `DISCOVERY_REQUIRED` are already out of active service and have their
#: own paths back to `ACTIVE` (promotion/discovery), so a second
#: rediscovery trigger on an already-`DEGRADED` profile is a no-op here
#: (idempotent — the guarded `UPDATE`'s `rowcount` is simply 0).
_DEGRADABLE_STATUS = StrategyStatus.ACTIVE


@dataclass(frozen=True)
class CombinedStats:
    """Aggregate/counter signal source for conditions 1-2 (FR-020a(a)).

    `recent_failure_count` is the profile's own counter (incremented on a
    preferred-method failure, reset on a qualifying success, Clarification
    #2). `success_rate` is the preferred method's cumulative success rate
    from persisted `strategy_attempt_stats` **plus pending buffered
    deltas** (FR-024) — `None` when no stats exist yet for the preferred
    method (never evaluated as "below floor" in that case). `avg_confidence`
    is the optional combined-stats fallback for condition 4 (FR-020a(b):
    "may use recent_signals confidence and/or combined avg_confidence").
    """

    recent_failure_count: int
    success_rate: Decimal | None
    avg_confidence: Decimal | None = None


@dataclass(frozen=True)
class RecentAttemptSignal:
    """One outcome for the profile's preferred method, most-recent-first
    (contracts/rediscovery.md "RecentSignals: last-N consecutive
    error_code, HTTP status, extracted price, currency-present flag,
    confidence, observed URL").

    `price_unrealistic` is precomputed by :func:`build_recent_signals`
    (the §18 bounds re-check, FR-020b) since it needs the match's
    configured `validation_rules` — external context `evaluate_rediscovery`
    itself never fetches (it stays pure/no-I/O). Every other field is used
    by `evaluate_rediscovery` directly (conditions 3/5/6 from `error_code`/
    `currency_present`; condition 8 re-derives `url_pattern` from `url`
    itself, reusing the shipped `derive_url_pattern`).
    """

    error_code: ScrapeErrorCode | None
    status_code: int | None
    price: Decimal | None
    currency_present: bool
    confidence: Decimal | None
    url: str | None
    price_unrealistic: bool = False


@dataclass(frozen=True)
class RecentSignals:
    """Per-attempt-outcome signal source for conditions 3,4,5,6,7,8
    (FR-020a(b)). `attempts` is ordered **most-recent-first** — the order
    `evaluate_rediscovery`'s consecutive-occurrence counting walks."""

    attempts: tuple[RecentAttemptSignal, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RediscoveryThresholds:
    """Rediscovery boundary knobs (`Settings.STRATEGY_REDISCOVERY_*`, data-model §7).

    `consecutive_occurrence` is the shared "repeatedly" threshold (default
    3, FR-020) for the six per-attempt-outcome conditions (3, 4 recent
    variant, 5, 6, 7, 8) — a single knob, not one per condition, per the
    spec's "a configurable consecutive-occurrence threshold (default 3)".
    """

    consecutive_failures: int
    success_rate_floor: Decimal
    low_confidence: Decimal
    consecutive_occurrence: int = 3


@dataclass(frozen=True)
class RediscoveryDecision:
    """Outcome of :func:`evaluate_rediscovery` for one profile (contracts/rediscovery.md)."""

    trigger: bool
    reason: str | None


def _consecutive(
    attempts: Sequence[RecentAttemptSignal], predicate: Callable[[RecentAttemptSignal], bool]
) -> int:
    """Count the leading (most-recent-first) run of `attempts` matching
    `predicate`, stopping at the first non-match. A genuine qualifying
    success never matches any of the six failure predicates below, so
    hitting one ends the streak — the "reset on a qualifying success"
    behaviour (FR-020) falls out of this definition directly, no separate
    success flag needed."""
    count = 0
    for attempt in attempts:
        if not predicate(attempt):
            break
        count += 1
    return count


def evaluate_rediscovery(
    profile: DomainStrategyProfile,
    combined_stats: CombinedStats,
    recent_signals: RecentSignals,
    thresholds: RediscoveryThresholds,
) -> RediscoveryDecision:
    """Any one of the 8 FR-020 conditions firing -> `trigger=True` (US4 AS1/AS2/AS3).

    Conditions 1-2 read `combined_stats` (aggregate/counter, FR-020a(a));
    conditions 3,5,6,7,8 read `recent_signals` (per-attempt-outcome,
    FR-020a(b)), each gated by `thresholds.consecutive_occurrence`;
    condition 4 (confidence) checks both sources (FR-020a(b) "may use
    recent_signals confidence and/or combined avg_confidence"). Healthy
    signals (rate >= floor, no consecutive failures, confidence >=
    threshold) -> `trigger=False`, profile stays `ACTIVE`.

    Pure — no I/O, no Scrapy/Twisted/FastAPI import (Constitution I/V).
    """
    threshold = thresholds.consecutive_occurrence

    # --- Condition 1: consecutive preferred-method failures. ---
    if combined_stats.recent_failure_count >= thresholds.consecutive_failures:
        return RediscoveryDecision(
            trigger=True,
            reason=(
                f"recent_failure_count={combined_stats.recent_failure_count} >= "
                f"consecutive_failures={thresholds.consecutive_failures}"
            ),
        )

    # --- Condition 2: cumulative success rate (persisted + pending). ---
    if (
        combined_stats.success_rate is not None
        and combined_stats.success_rate < thresholds.success_rate_floor
    ):
        return RediscoveryDecision(
            trigger=True,
            reason=(
                f"success_rate={combined_stats.success_rate} < "
                f"success_rate_floor={thresholds.success_rate_floor}"
            ),
        )

    attempts = recent_signals.attempts

    # --- Condition 3: selector returns empty repeatedly. ---
    empty_selector = _consecutive(attempts, lambda a: a.error_code in _EMPTY_SELECTOR_CODES)
    if empty_selector >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_empty_selector={empty_selector} >= threshold={threshold}",
        )

    # --- Condition 4: price confidence below threshold repeatedly. ---
    low_confidence = _consecutive(
        attempts, lambda a: a.confidence is not None and a.confidence < thresholds.low_confidence
    )
    if low_confidence >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_low_confidence={low_confidence} >= threshold={threshold}",
        )
    if (
        combined_stats.avg_confidence is not None
        and combined_stats.avg_confidence < thresholds.low_confidence
    ):
        return RediscoveryDecision(
            trigger=True,
            reason=(
                f"avg_confidence={combined_stats.avg_confidence} < "
                f"low_confidence={thresholds.low_confidence}"
            ),
        )

    # --- Condition 5: repeated 403/429. ---
    blocked = _consecutive(attempts, lambda a: a.error_code in _BLOCKED_CODES)
    if blocked >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_403_429={blocked} >= threshold={threshold}",
        )

    # --- Condition 6: required currency absent repeatedly. ---
    currency_absent = _consecutive(attempts, lambda a: not a.currency_present)
    if currency_absent >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_currency_absent={currency_absent} >= threshold={threshold}",
        )

    # --- Condition 7 (FR-020b): price values become unrealistic (§18 bounds). ---
    unrealistic_price = _consecutive(attempts, lambda a: a.price_unrealistic)
    if unrealistic_price >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_unrealistic_price={unrealistic_price} >= threshold={threshold}",
        )

    # --- Condition 8 (FR-020b): template appears changed (re-derived pattern). ---
    template_changed = _consecutive(
        attempts,
        lambda a: a.url is not None and derive_url_pattern(a.url) != profile.url_pattern,
    )
    if template_changed >= threshold:
        return RediscoveryDecision(
            trigger=True,
            reason=f"consecutive_template_changed={template_changed} >= threshold={threshold}",
        )

    return RediscoveryDecision(
        trigger=False,
        reason=(
            "healthy signals: success_rate>=floor, no consecutive failures, "
            "confidence>=threshold"
        ),
    )


def _price_fails_bounds(price: Decimal, validation_rules: dict | None) -> bool:
    """Re-check `price` against the configured `min_price`/`max_price`
    §18 bounds (FR-020b "unrealistic" rule) — the same `parse_money`
    boundary primitive and comparison `scrape_core.validation
    .validate_candidate`'s bounds step (step 4) uses, duplicated locally
    (module docstring "§18 bounds re-check without a scrape_core import").
    `False` (never "unrealistic") when no bounds are configured — without
    a configured bound, "unrealistic" can't be judged (a permissive
    default, not a false trigger)."""
    if not validation_rules:
        return False
    min_price = validation_rules.get("min_price")
    max_price = validation_rules.get("max_price")
    if min_price is None and max_price is None:
        return False
    try:
        if min_price is not None and price < parse_money(min_price, non_negative=True):
            return True
        if max_price is not None and price > parse_money(max_price, non_negative=True):
            return True
    except (TypeError, ValueError, InvalidOperation):
        # A malformed configured bound can't be used to judge "unrealistic"
        # -- fail open on this one signal, other conditions still apply.
        return False
    return False


def build_recent_signals(
    session: Session,
    profile: DomainStrategyProfile,
    *,
    limit: int = _DEFAULT_RECENT_SIGNAL_LIMIT,
) -> RecentSignals:
    """Assemble :class:`RecentSignals` for `profile`'s preferred access
    method from the last `limit` `request_attempts` rows (contracts/
    rediscovery.md "Call sites" — the flush task and the periodic light
    re-check, both off the hot path, never the reactor/spider).

    Reads `request_attempts` for matches under this profile's `(workspace,
    competitor, url_pattern)` key (`competitor_product_matches`, the same
    key the profile itself is resolved by), restricted to the profile's
    `preferred_access_method` (the "preferred method" FR-020a(b) scopes
    every per-attempt-outcome condition to), most-recent first. Each
    attempt is paired (`request_attempts.created_at ==
    price_observations.scraped_at`, same `match_id`/`workspace_id` — the
    exact correlation `scrape_core.pipelines._flush_batch` writes both
    rows under, one `moment` per item) with its `price_observations` row
    for price/currency/confidence, and with the match's
    `scrape_profiles.validation_rules` (via `competitor_product_matches
    .scrape_profile_id`) for the condition-7 §18 bounds re-check.

    Returns an empty `RecentSignals` when the profile has no
    `preferred_access_method` yet (nothing to evaluate outcome conditions
    against — conditions 1-2 via `combined_stats` still apply upstream).

    **Blocking** (SQLAlchemy reads) — must only ever be called from an
    already off-reactor context (a Celery task), never on the reactor
    thread (Constitution V).
    """
    if profile.preferred_access_method is None:
        return RecentSignals(attempts=())

    stmt = (
        select(RequestAttempt, PriceObservation, ScrapeProfile.validation_rules)
        .join(
            CompetitorProductMatch,
            (CompetitorProductMatch.workspace_id == RequestAttempt.workspace_id)
            & (CompetitorProductMatch.id == RequestAttempt.match_id),
        )
        .outerjoin(
            PriceObservation,
            (PriceObservation.workspace_id == RequestAttempt.workspace_id)
            & (PriceObservation.match_id == RequestAttempt.match_id)
            & (PriceObservation.scraped_at == RequestAttempt.created_at),
        )
        .outerjoin(ScrapeProfile, ScrapeProfile.id == CompetitorProductMatch.scrape_profile_id)
        .where(
            RequestAttempt.workspace_id == profile.workspace_id,
            CompetitorProductMatch.competitor_id == profile.competitor_id,
            CompetitorProductMatch.url_pattern == profile.url_pattern,
            RequestAttempt.access_method == profile.preferred_access_method,
        )
        .order_by(RequestAttempt.created_at.desc())
        .limit(limit)
    )

    attempts: list[RecentAttemptSignal] = []
    for request_attempt, observation, validation_rules in session.execute(stmt).all():
        price = observation.price if observation is not None else None
        currency_present = observation is not None and observation.currency is not None
        confidence = observation.extraction_confidence if observation is not None else None
        price_unrealistic = (
            _price_fails_bounds(price, validation_rules) if price is not None else False
        )
        attempts.append(
            RecentAttemptSignal(
                error_code=request_attempt.error_code,
                status_code=request_attempt.status_code,
                price=price,
                currency_present=currency_present,
                confidence=confidence,
                url=request_attempt.url,
                price_unrealistic=price_unrealistic,
            )
        )

    return RecentSignals(attempts=tuple(attempts))


def apply_rediscovery(
    session: Session,
    profile: DomainStrategyProfile,
    decision: RediscoveryDecision,
) -> bool:
    """Guarded atomic apply of a rediscovery trigger (contracts/rediscovery.md
    "Apply", FR-020, SC-004; used by the flush task, US5 T035, and the
    periodic light re-check, T031).

    No-op (`False`, no statement executed) when `decision.trigger` is
    `False`. Otherwise issues exactly one atomic `UPDATE
    domain_strategy_profiles SET status='DEGRADED', last_failed_at=:now
    WHERE id=:pid AND status='ACTIVE'` — a `DISABLED` profile (or one
    already `DEGRADED`/`LEARNING`/`DISCOVERY_REQUIRED`) is never
    rediscovered by this path (US4 AS1). On a genuine transition
    (`rowcount > 0`), enqueues `STRATEGY_DISCOVERY_RUN` on the
    `strategy_discovery` queue for this profile's `(workspace, competitor,
    domain, url_pattern)` key (SC-004: degraded + enqueued within one
    evaluation cycle) and emits `strategy_rediscovery_triggered`
    (Constitution §31, contracts/observability.md).

    Returns `True` iff this call's UPDATE actually changed a row — so two
    concurrent evaluations of the same profile (inline flush + periodic
    re-check racing) enqueue at most one discovery run.
    """
    if not decision.trigger:
        return False

    now = datetime.now(timezone.utc)
    stmt = (
        update(DomainStrategyProfile)
        .where(
            DomainStrategyProfile.id == profile.id,
            DomainStrategyProfile.status == _DEGRADABLE_STATUS,
        )
        .values(status=StrategyStatus.DEGRADED, last_failed_at=now)
    )
    result = session.execute(stmt)
    if not (result.rowcount and result.rowcount > 0):
        return False

    enqueue(
        STRATEGY_DISCOVERY_RUN,
        queue=_DISCOVERY_QUEUE,
        kwargs={
            "workspace_id": str(profile.workspace_id),
            "competitor_id": str(profile.competitor_id),
            "domain": profile.domain,
            "url_pattern": profile.url_pattern,
            "sample_urls": [],
            "triggered_by": "REDISCOVERY",
        },
    )
    logger.info(
        "app_shared.strategy.rediscovery: strategy_rediscovery_triggered "
        "profile_id=%s workspace_id=%s reason=%s",
        profile.id,
        profile.workspace_id,
        decision.reason,
    )
    return True
