"""Pure resolution and circuit logic for versioned strategy-method chains.

The resolver operates on ORM rows but performs no I/O.  Keeping the decision
pure makes the same ordered chain usable by HTTP dispatch, browser dispatch,
workers, canaries, and API previews without embedding competitor names or
product assumptions in orchestration code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol

from app_shared.enums import (
    AccessMethod,
    ScrapeErrorCode,
    ScrapeProfileMode,
    StrategyMethodProofState,
)

__all__ = [
    "CircuitPolicy",
    "MethodCircuitSnapshot",
    "StrategyMethodSelection",
    "evolve_method_circuit",
    "is_method_health_failure",
    "mode_for_access_method",
    "resolve_method_candidate",
]


class StrategyMethodLike(Protocol):
    id: uuid.UUID
    access_method: AccessMethod
    priority: int
    enter_on: list
    fallback_on: list
    enabled: bool
    proof_state: StrategyMethodProofState
    cooldown_until: datetime | None
    next_canary_at: datetime | None
    retired_at: datetime | None


@dataclass(frozen=True)
class StrategyMethodSelection:
    method: StrategyMethodLike
    attempt_ordinal: int
    is_canary: bool = False


def mode_for_access_method(access_method: AccessMethod) -> ScrapeProfileMode:
    """Derive node mode from the selected candidate's actual transport."""
    if access_method in (
        AccessMethod.PLAYWRIGHT_DIRECT,
        AccessMethod.PLAYWRIGHT_PROXY,
    ):
        return ScrapeProfileMode.BROWSER
    return ScrapeProfileMode.HTTP


_PERMANENT_OUTCOMES = frozenset(
    {
        ScrapeErrorCode.NOT_LISTED,
        ScrapeErrorCode.IDENTITY_MISMATCH,
        ScrapeErrorCode.POLICY_BLOCKED,
    }
)
_PROXY_METHODS = frozenset(
    {AccessMethod.PROXY_HTTP, AccessMethod.PLAYWRIGHT_PROXY}
)


def _fallback_matches(method: StrategyMethodLike, outcome: ScrapeErrorCode) -> bool:
    configured = {
        value.value if isinstance(value, ScrapeErrorCode) else str(value)
        for value in (method.fallback_on or [])
    }
    return "*" in configured or outcome.value in configured


def _entry_matches(method: StrategyMethodLike, outcome: ScrapeErrorCode) -> bool:
    configured = {
        value.value if isinstance(value, ScrapeErrorCode) else str(value)
        for value in (getattr(method, "enter_on", None) or [])
    }
    return not configured or "*" in configured or outcome.value in configured


def _is_eligible(
    method: StrategyMethodLike,
    *,
    now: datetime,
    open_method_ids: frozenset[uuid.UUID],
    proxy_budget_exhausted: bool,
) -> tuple[bool, bool]:
    if not method.enabled or method.retired_at is not None:
        return False, False
    if method.proof_state is StrategyMethodProofState.DISABLED:
        return False, False
    if proxy_budget_exhausted and method.access_method in _PROXY_METHODS:
        return False, False

    canary_due = method.next_canary_at is not None and method.next_canary_at <= now
    if method.proof_state is StrategyMethodProofState.QUARANTINED:
        return canary_due, canary_due
    if method.id in open_method_ids:
        return canary_due, canary_due
    if method.cooldown_until is not None and method.cooldown_until > now:
        return canary_due, canary_due
    return True, False


def resolve_method_candidate(
    methods: Iterable[StrategyMethodLike],
    *,
    preferred_method_id: uuid.UUID | None = None,
    current_method_id: uuid.UUID | None = None,
    outcome: ScrapeErrorCode | None = None,
    current_attempt_ordinal: int = 0,
    now: datetime | None = None,
    open_method_ids: frozenset[uuid.UUID] = frozenset(),
    proxy_budget_exhausted: bool = False,
) -> StrategyMethodSelection | None:
    """Select the first runnable method after an outcome-conditioned cursor.

    A permanent policy/identity/not-listed outcome ends the chain.  Other
    outcomes only advance when the current method explicitly lists them in
    ``fallback_on`` (or ``*``), ensuring configuration—not customer-specific
    code—owns escalation behavior.
    """
    now = now or datetime.now(timezone.utc)
    ordered = sorted(methods, key=lambda method: (method.priority, str(method.id)))
    if not ordered:
        return None

    if current_method_id is None:
        if preferred_method_id is not None:
            ordered.sort(
                key=lambda method: (
                    method.id != preferred_method_id,
                    method.priority,
                    str(method.id),
                )
            )
        candidates = ordered
    else:
        current_index = next(
            (index for index, method in enumerate(ordered) if method.id == current_method_id),
            None,
        )
        # A stale/unknown cursor must fail closed; restarting at priority 1
        # would duplicate paid work after a configuration revision.
        if current_index is None or outcome is None or outcome in _PERMANENT_OUTCOMES:
            return None
        current = ordered[current_index]
        if not _fallback_matches(current, outcome):
            return None
        candidates = ordered[current_index + 1 :]

    for method in candidates:
        if outcome is not None and not _entry_matches(method, outcome):
            continue
        eligible, is_canary = _is_eligible(
            method,
            now=now,
            open_method_ids=open_method_ids,
            proxy_budget_exhausted=proxy_budget_exhausted,
        )
        if eligible:
            return StrategyMethodSelection(
                method=method,
                attempt_ordinal=current_attempt_ordinal + 1,
                is_canary=is_canary,
            )
    return None


@dataclass(frozen=True)
class CircuitPolicy:
    failure_streak: int = 3
    minimum_attempts: int = 10
    failure_rate: float = 0.8
    cooldown: timedelta = timedelta(minutes=30)
    canary_interval: timedelta = timedelta(minutes=10)


@dataclass(frozen=True)
class MethodCircuitSnapshot:
    attempts: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    quarantined: bool = False
    cooldown_until: datetime | None = None
    next_canary_at: datetime | None = None


_OPERATIONAL_FAILURES = frozenset(
    {
        ScrapeErrorCode.PRICE_NOT_FOUND,
        ScrapeErrorCode.SELECTOR_BROKEN,
        ScrapeErrorCode.HTTP_403,
        ScrapeErrorCode.HTTP_429,
        ScrapeErrorCode.BLOCKED,
        ScrapeErrorCode.TIMEOUT,
        ScrapeErrorCode.PROXY_FAILED,
        ScrapeErrorCode.CONNECTION_FAILED,
        ScrapeErrorCode.TLS_CONNECTION_FAILED,
        ScrapeErrorCode.TLS_VERIFICATION_FAILED,
        ScrapeErrorCode.PROTOCOL_FAILED,
        ScrapeErrorCode.PLAYWRIGHT_FAILED,
    }
)


def is_method_health_failure(outcome: ScrapeErrorCode | None) -> bool:
    """Whether an outcome is evidence that this method—not the listing—is unhealthy."""
    return outcome in _OPERATIONAL_FAILURES


def evolve_method_circuit(
    snapshot: MethodCircuitSnapshot,
    *,
    outcome: ScrapeErrorCode | None,
    success: bool,
    policy: CircuitPolicy = CircuitPolicy(),
    now: datetime | None = None,
) -> MethodCircuitSnapshot:
    """Advance one `(domain, strategy_method)` breaker snapshot.

    Permanent catalog/policy outcomes are deliberately not method-health
    failures.  A successful canary closes its method circuit without
    affecting any other method for the same domain.
    """
    now = now or datetime.now(timezone.utc)
    attempts = snapshot.attempts + 1
    if success:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=snapshot.failures,
            consecutive_failures=0,
            quarantined=False,
        )
    if outcome not in _OPERATIONAL_FAILURES:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=snapshot.failures,
            consecutive_failures=0,
            quarantined=snapshot.quarantined,
            cooldown_until=snapshot.cooldown_until,
            next_canary_at=snapshot.next_canary_at,
        )

    failures = snapshot.failures + 1
    streak = snapshot.consecutive_failures + 1
    rate = failures / attempts
    should_open = streak >= policy.failure_streak or (
        attempts >= policy.minimum_attempts and rate >= policy.failure_rate
    )
    if should_open:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=failures,
            consecutive_failures=streak,
            quarantined=True,
            cooldown_until=now + policy.cooldown,
            next_canary_at=now + policy.canary_interval,
        )
    return MethodCircuitSnapshot(
        attempts=attempts,
        failures=failures,
        consecutive_failures=streak,
        quarantined=snapshot.quarantined,
        cooldown_until=snapshot.cooldown_until,
        next_canary_at=snapshot.next_canary_at,
    )
