"""Atomic Redis -> Postgres flush of buffered attempt stats
(`contracts/stats-buffer.md` §Flush, FR-012/FR-023/FR-024, US5).

`flush_profile(session, redis, profile_id)` is the convergence point US5
promises: for every `(method_type, method_name)` key of one profile, it
drains the Redis buffer (`app_shared.strategy.stats_buffer.drain`) and
issues a **single** atomic `INSERT ... ON CONFLICT DO UPDATE SET count =
count + EXCLUDED.count` per key (no app-side read-modify-write, FR-023),
then evaluates promotion (`app_shared.strategy.promotion`, US1) and
rediscovery (`app_shared.strategy.rediscovery`, US4) against the
just-flushed persisted counts. Called by the `STRATEGY_STATS_FLUSH`
Celery task (periodic + job-finalization, `apps/workers/app/workers/
tasks_strategy.py`) — always off-reactor (a Celery task / worker
context), never the spider/reactor thread.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, cast, func, literal
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app_shared.config import Settings, get_settings
from app_shared.database import set_workspace_context
from app_shared.enums import AccessMethod, ExtractionMethod, MethodType, StrategyStatus
from app_shared.ids import new_uuid7
from app_shared.models.strategy import DomainStrategyProfile, StrategyAttemptStats
from app_shared.strategy import stats_buffer
from app_shared.strategy.promotion import (
    MethodStats,
    PromotionThresholds,
    apply_promotion,
    evaluate_promotion,
)
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RediscoveryThresholds,
    apply_rediscovery,
    build_recent_signals,
    evaluate_rediscovery,
)
from app_shared.strategy.repository import stats_for_profile

__all__ = ["flush_profile", "FlushResult", "StrategyTransition"]


@dataclass(frozen=True)
class StrategyTransition:
    """One genuine promotion/rediscovery transition surfaced by `flush_profile`
    (SPEC-16 US3 T035, contracts/events.md #3) -- only ever constructed when
    the corresponding `apply_promotion`/`apply_rediscovery` call returned
    `True` (a real row change), so this is never emitted speculatively.
    """

    profile_id: uuid.UUID
    workspace_id: uuid.UUID
    domain: str
    new_status: StrategyStatus
    change: str  # "PROMOTED" | "REDISCOVERY_TRIGGERED"
    method: str | None


@dataclass(frozen=True)
class FlushResult:
    """`flush_profile`'s return: the number of keys drained this cycle plus
    any genuine strategy-status transitions it caused (SPEC-16 US3 T035) --
    the caller (`tasks_strategy.py::flush_stats`) enqueues one webhook event
    per transition, strictly after its own `session.commit()`.
    """

    keys_flushed: int
    transitions: tuple[StrategyTransition, ...] = field(default_factory=tuple)


#: Scale factor `conf_sum` was multiplied by at record time
#: (`stats_buffer._CONFIDENCE_SCALE`) -- duplicated here (a plain int
#: constant, not worth importing a private name across the module
#: boundary) to unscale it back to a `[0, 1]` `Decimal` at flush.
_CONFIDENCE_SCALE = 10_000

#: Every `(method_type, candidate method_name)` combo ever recordable
#: (D1: `method_name` is always a reused `AccessMethod`/`ExtractionMethod`
#: value). `stratdirty:{workspace_id}` only tracks *which profile* has a
#: pending delta, not which of its method keys -- so a flush walks this
#: small, fully-enumerated cross product (4 + 8 = 12 keys) and skips any
#: combo whose drain comes back empty (`attempt == 0`), rather than
#: maintaining a second per-profile dirty-key-family index.
_CANDIDATE_METHODS: tuple[tuple[MethodType, str], ...] = tuple(
    (MethodType.ACCESS, member.value) for member in AccessMethod
) + tuple((MethodType.EXTRACTION, member.value) for member in ExtractionMethod)


def _promotion_thresholds(settings: Settings) -> PromotionThresholds:
    return PromotionThresholds(
        min_successes=settings.STRATEGY_PROMOTION_MIN_SUCCESSES,
        min_distinct_urls=settings.STRATEGY_PROMOTION_MIN_DISTINCT_URLS,
        confidence_threshold=Decimal(str(settings.STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD)),
    )


def _rediscovery_thresholds(settings: Settings) -> RediscoveryThresholds:
    return RediscoveryThresholds(
        consecutive_failures=settings.STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES,
        success_rate_floor=Decimal(str(settings.STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR)),
        low_confidence=Decimal(str(settings.STRATEGY_REDISCOVERY_LOW_CONFIDENCE)),
    )


def _upsert_stats(
    session: Session,
    profile_id: uuid.UUID,
    method_type: MethodType,
    method_name: str,
    drained: stats_buffer.DrainedDelta,
    now: datetime,
) -> Any:
    """Single atomic `count = count + delta` UPSERT (FR-023) -- no
    app-side read-modify-write. Every `SET` expression is computed
    server-side from the row's pre-update value (an unqualified column
    reference) combined with this call's delta (`EXCLUDED.*` for the
    three raw counters persisted as columns; a bound literal for
    `rt_ms_sum`/`conf_sum`, which are buffer-only quantities with no
    persisted column of their own -- only their running *averages* are
    persisted). Returns the post-upsert row (`RETURNING`) so the caller
    can feed `combined.confidence` to `evaluate_promotion` without a
    second round-trip.
    """
    table = StrategyAttemptStats

    success_rate = (
        (Decimal(drained.success) / Decimal(drained.attempt)) if drained.attempt else Decimal("0")
    )
    avg_response_time_ms = (drained.rt_ms_sum // drained.attempt) if drained.attempt else None
    avg_confidence = (
        (Decimal(drained.conf_sum) / _CONFIDENCE_SCALE / drained.success) if drained.success else None
    )

    stmt = pg_insert(table).values(
        id=new_uuid7(),
        domain_strategy_profile_id=profile_id,
        method_type=method_type,
        method_name=method_name,
        attempt_count=drained.attempt,
        success_count=drained.success,
        failure_count=drained.failure,
        success_rate=success_rate,
        avg_response_time_ms=avg_response_time_ms,
        avg_confidence=avg_confidence,
        last_success_at=now if drained.success else None,
        last_failed_at=now if drained.failure else None,
    )

    new_attempt = table.attempt_count + stmt.excluded.attempt_count
    new_success = table.success_count + stmt.excluded.success_count

    stmt = stmt.on_conflict_do_update(
        index_elements=["domain_strategy_profile_id", "method_type", "method_name"],
        set_={
            "attempt_count": new_attempt,
            "success_count": new_success,
            "failure_count": table.failure_count + stmt.excluded.failure_count,
            "success_rate": cast(new_success, Numeric(5, 4)) / func.nullif(new_attempt, 0),
            "avg_response_time_ms": (
                (func.coalesce(table.avg_response_time_ms, 0) * table.attempt_count)
                + literal(drained.rt_ms_sum)
            )
            / func.nullif(new_attempt, 0),
            "avg_confidence": (
                (func.coalesce(table.avg_confidence, 0) * table.success_count)
                + (cast(literal(drained.conf_sum), Numeric(18, 4)) / _CONFIDENCE_SCALE)
            )
            / func.nullif(new_success, 0),
            "last_success_at": func.greatest(table.last_success_at, stmt.excluded.last_success_at),
            "last_failed_at": func.greatest(table.last_failed_at, stmt.excluded.last_failed_at),
            "updated_at": func.now(),
        },
    ).returning(
        table.attempt_count,
        table.success_count,
        table.failure_count,
        table.success_rate,
        table.avg_confidence,
        table.avg_response_time_ms,
        table.last_success_at,
        table.last_failed_at,
    )

    return session.execute(stmt).one()


def _combined_stats(session: Session, profile: DomainStrategyProfile) -> CombinedStats:
    """`CombinedStats` for `evaluate_rediscovery` conditions 1-2 (FR-020a(a)),
    read straight from the just-upserted `strategy_attempt_stats` rows --
    by the time this runs every candidate key has already been drained
    this cycle, so "persisted" already *is* "persisted + pending" (FR-024,
    nothing is left buffered in Redis for this profile at this point). The
    worse (lower) of the preferred access/extraction `success_rate`s is
    used so degradation on *either* learned channel is caught (mirrors
    `tasks_strategy.py::_combined_stats_for_profile`'s periodic-path
    logic, duplicated locally rather than imported -- `app_shared` must
    never depend on `apps.workers`, Constitution I)."""
    rows = stats_for_profile(session, profile.workspace_id, profile.id)
    by_key = {(row.method_type, row.method_name): row for row in rows}

    success_rate: Decimal | None = None
    avg_confidence: Decimal | None = None
    for method_type, method_name in (
        (MethodType.ACCESS, profile.preferred_access_method),
        (MethodType.EXTRACTION, profile.preferred_extraction_method),
    ):
        if method_name is None:
            continue
        row = by_key.get((method_type, method_name))
        if row is None:
            continue
        if success_rate is None or row.success_rate < success_rate:
            success_rate = row.success_rate
        if method_type is MethodType.EXTRACTION:
            avg_confidence = row.avg_confidence

    return CombinedStats(
        recent_failure_count=profile.recent_failure_count,
        success_rate=success_rate,
        avg_confidence=avg_confidence,
    )


def flush_profile(session: Session, redis: Any, profile_id: uuid.UUID | str) -> FlushResult:
    """Drain + upsert every dirty `(method_type, method_name)` key of one
    profile, then evaluate promotion (US1) and rediscovery (US4) against
    the just-flushed persisted counts (contracts/stats-buffer.md §Flush).

    Returns a `FlushResult` whose `keys_flushed` is the number of
    `(method_type, method_name)` keys that actually had a pending delta
    this cycle (0 if the profile had nothing buffered -- e.g. a stale
    `stratdirty` member from an already-drained cycle), and whose
    `transitions` (SPEC-16 US3 T035, contracts/events.md #3) surfaces every
    genuine promotion/rediscovery status change this call caused -- only
    when the corresponding `apply_promotion`/`apply_rediscovery` call
    actually returned `True` (a real row change), never speculatively.
    `profile_id` alone resolves the profile (no `workspace_id` argument)
    -- the *first* thing this does is load the row by bare PK and
    `set_workspace_context` from its own `workspace_id`, the same
    "resolve first, scope second" shape `apps/workers/app/workers/
    tasks_strategy.py::light_recheck` already uses for its own per-profile
    loop (this module's one unscoped read of a workspace-owned model).

    No-op (`FlushResult(0, ())`, no statement executed) when the profile no
    longer exists (e.g. deleted between the dirty-set read and this flush).
    """
    profile = session.get(DomainStrategyProfile, profile_id)  # noqa: workspace-scope
    if profile is None:
        return FlushResult(keys_flushed=0, transitions=())

    set_workspace_context(session, profile.workspace_id)

    settings = get_settings()
    promotion_thresholds = _promotion_thresholds(settings)
    now = datetime.now(timezone.utc)

    keys_flushed = 0
    any_success = False
    any_failure = False
    preferred_qualifying_delta = 0
    preferred_failure_delta = 0
    transitions: list[StrategyTransition] = []

    for method_type, method_name in _CANDIDATE_METHODS:
        drained = stats_buffer.drain(
            redis, profile_id=profile.id, method_type=method_type, method_name=method_name
        )
        if drained.attempt == 0:
            continue  # nothing pending for this key this cycle

        keys_flushed += 1
        row = _upsert_stats(session, profile.id, method_type, method_name, drained, now)

        if drained.success:
            any_success = True
        if drained.failure:
            any_failure = True

        is_preferred_method = (
            method_type is MethodType.ACCESS and method_name == profile.preferred_access_method
        ) or (
            method_type is MethodType.EXTRACTION
            and method_name == profile.preferred_extraction_method
        )
        if is_preferred_method:
            preferred_qualifying_delta += drained.qualifying_success
            preferred_failure_delta += drained.failure

        # Promotion is evaluated per-method regardless of "preferred"
        # status -- any candidate method crossing the bar promotes (US1
        # AS1); access and extraction are independent (US1 AS5).
        combined = MethodStats(
            qualifying_success_count=drained.qualifying_success,
            confidence=row.avg_confidence,
        )
        decision = evaluate_promotion(combined, drained.distinct_urls, promotion_thresholds)
        promoted = apply_promotion(
            session,
            profile.id,
            method_type=method_type,
            method_name=method_name,
            decision=decision,
        )
        if promoted:
            # The distinct-URL SET is the running promotion evidence --
            # only cleared once the method actually promotes
            # (contracts/stats-buffer.md "Drain").
            redis.delete(stats_buffer.url_key(profile.id, method_type, method_name))
            # SPEC-16 US3 (T035a): a genuine promotion -- `apply_promotion`
            # only returns True on a real row change -- surfaced to the
            # caller for a post-commit webhook enqueue.
            transitions.append(
                StrategyTransition(
                    profile_id=profile.id,
                    workspace_id=profile.workspace_id,
                    domain=profile.domain,
                    new_status=StrategyStatus.ACTIVE,
                    change="PROMOTED",
                    method=method_name,
                )
            )

    # recent_failure_count (Clarification #2): ++ on a preferred-method
    # failure delta, reset to 0 on a preferred-method qualifying-success
    # delta (a qualifying success always wins the race within one batched
    # delta -- it breaks the streak regardless of how many failures also
    # landed in the same flush window).
    if preferred_qualifying_delta > 0:
        profile.recent_failure_count = 0
    elif preferred_failure_delta > 0:
        profile.recent_failure_count += preferred_failure_delta

    if any_success:
        profile.last_success_at = now
    if any_failure:
        profile.last_failed_at = now

    # Rediscovery (US4): persisted + pending (already merged above, FR-024)
    # combined counts, plus a freshly built recent_signals off the hot path.
    combined_stats = _combined_stats(session, profile)
    recent_signals = build_recent_signals(session, profile)
    rediscovery_decision = evaluate_rediscovery(
        profile, combined_stats, recent_signals, _rediscovery_thresholds(settings)
    )
    rediscovered = apply_rediscovery(session, profile, rediscovery_decision)
    if rediscovered:
        # SPEC-16 US3 (T035a): a genuine rediscovery trigger -- surfaced to
        # the caller for a post-commit webhook enqueue, same as promotion.
        transitions.append(
            StrategyTransition(
                profile_id=profile.id,
                workspace_id=profile.workspace_id,
                domain=profile.domain,
                new_status=StrategyStatus.DEGRADED,
                change="REDISCOVERY_TRIGGERED",
                method=None,
            )
        )

    # Every candidate key was drained this cycle -- by definition nothing
    # is left pending for this profile from here, so the dirty marker is
    # always cleared (contracts/stats-buffer.md step 5, "once no pending
    # deltas remain"); a `record_attempt` racing in after this drain
    # re-dirties the profile via its own SADD, picked up next cycle.
    redis.srem(stats_buffer.dirty_key(profile.workspace_id), str(profile.id))

    return FlushResult(keys_flushed=keys_flushed, transitions=tuple(transitions))
