from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app_shared.enums import (
    AccessMethod,
    ScrapeErrorCode,
    ScrapeProfileMode,
    StrategyMethodProofState,
)
from app_shared.strategy.methods import (
    CircuitPolicy,
    MethodCircuitSnapshot,
    evolve_method_circuit,
    mode_for_access_method,
    resolve_method_candidate,
)


@dataclass
class _Method:
    priority: int
    access_method: AccessMethod
    fallback_on: list[str]
    enter_on: list[str] | None = None
    id: uuid.UUID = uuid.uuid4()
    enabled: bool = True
    proof_state: StrategyMethodProofState = StrategyMethodProofState.PROVEN
    cooldown_until: datetime | None = None
    next_canary_at: datetime | None = None
    retired_at: datetime | None = None


def _method(priority: int, access: AccessMethod, *fallback: ScrapeErrorCode) -> _Method:
    return _Method(
        id=uuid.uuid4(),
        priority=priority,
        access_method=access,
        fallback_on=[value.value for value in fallback],
    )


def test_initial_resolution_uses_preferred_then_priority() -> None:
    first = _method(1, AccessMethod.DIRECT_HTTP)
    preferred = _method(2, AccessMethod.PLAYWRIGHT_DIRECT)
    selected = resolve_method_candidate(
        [first, preferred], preferred_method_id=preferred.id
    )
    assert selected is not None
    assert selected.method is preferred
    assert selected.attempt_ordinal == 1


def test_price_not_found_advances_to_browser_when_configured() -> None:
    raw = _method(
        1,
        AccessMethod.DIRECT_HTTP,
        ScrapeErrorCode.PRICE_NOT_FOUND,
    )
    browser = _method(2, AccessMethod.PLAYWRIGHT_DIRECT)
    selected = resolve_method_candidate(
        [raw, browser],
        current_method_id=raw.id,
        outcome=ScrapeErrorCode.PRICE_NOT_FOUND,
        current_attempt_ordinal=1,
    )
    assert selected is not None
    assert selected.method is browser
    assert selected.attempt_ordinal == 2
    assert mode_for_access_method(selected.method.access_method) is ScrapeProfileMode.BROWSER


def test_outcome_not_in_fallback_set_ends_chain() -> None:
    raw = _method(1, AccessMethod.DIRECT_HTTP, ScrapeErrorCode.HTTP_403)
    assert (
        resolve_method_candidate(
            [raw, _method(2, AccessMethod.PROXY_HTTP)],
            current_method_id=raw.id,
            outcome=ScrapeErrorCode.PRICE_NOT_FOUND,
        )
        is None
    )


def test_permanent_identity_and_policy_outcomes_never_retry() -> None:
    first = _method(1, AccessMethod.DIRECT_HTTP, ScrapeErrorCode.NOT_LISTED)
    second = _method(2, AccessMethod.PLAYWRIGHT_PROXY)
    for outcome in (
        ScrapeErrorCode.NOT_LISTED,
        ScrapeErrorCode.IDENTITY_MISMATCH,
        ScrapeErrorCode.POLICY_BLOCKED,
    ):
        assert (
            resolve_method_candidate(
                [first, second], current_method_id=first.id, outcome=outcome
            )
            is None
        )


def test_cooldown_and_proxy_budget_skip_only_affected_methods() -> None:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    cooled_proxy = _method(1, AccessMethod.PROXY_HTTP)
    cooled_proxy.cooldown_until = now + timedelta(minutes=5)
    browser_proxy = _method(2, AccessMethod.PLAYWRIGHT_PROXY)
    direct = _method(3, AccessMethod.DIRECT_HTTP_RETRY)
    selected = resolve_method_candidate(
        [cooled_proxy, browser_proxy, direct],
        now=now,
        proxy_budget_exhausted=True,
    )
    assert selected is not None
    assert selected.method is direct


def test_quarantined_method_only_runs_when_canary_is_due() -> None:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    quarantined = _method(1, AccessMethod.DIRECT_HTTP)
    quarantined.proof_state = StrategyMethodProofState.QUARANTINED
    quarantined.next_canary_at = now - timedelta(seconds=1)
    selected = resolve_method_candidate([quarantined], now=now)
    assert selected is not None
    assert selected.is_canary is True


def test_unknown_chain_cursor_fails_closed() -> None:
    assert (
        resolve_method_candidate(
            [_method(1, AccessMethod.DIRECT_HTTP)],
            current_method_id=uuid.uuid4(),
            outcome=ScrapeErrorCode.TIMEOUT,
        )
        is None
    )


def test_breaker_opens_per_method_after_failure_streak_and_canary_closes() -> None:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    state = MethodCircuitSnapshot()
    policy = CircuitPolicy(failure_streak=2)
    state = evolve_method_circuit(
        state,
        outcome=ScrapeErrorCode.HTTP_403,
        success=False,
        policy=policy,
        now=now,
    )
    assert state.quarantined is False
    state = evolve_method_circuit(
        state,
        outcome=ScrapeErrorCode.BLOCKED,
        success=False,
        policy=policy,
        now=now,
    )
    assert state.quarantined is True
    assert state.next_canary_at is not None

    recovered = evolve_method_circuit(
        state,
        outcome=None,
        success=True,
        policy=policy,
        now=now + timedelta(minutes=10),
    )
    assert recovered.quarantined is False
    assert recovered.consecutive_failures == 0


def test_not_listed_does_not_trip_method_health() -> None:
    state = evolve_method_circuit(
        MethodCircuitSnapshot(),
        outcome=ScrapeErrorCode.NOT_LISTED,
        success=False,
    )
    assert state.attempts == 1
    assert state.failures == 0
    assert state.quarantined is False


def test_outcome_selects_matching_branch_without_domain_code() -> None:
    current = _method(0, AccessMethod.DIRECT_HTTP, ScrapeErrorCode.HTTP_404)
    browser = _method(1, AccessMethod.PLAYWRIGHT_DIRECT)
    browser.enter_on = [ScrapeErrorCode.PRICE_NOT_FOUND.value, ScrapeErrorCode.TIMEOUT.value]
    repair = _method(2, AccessMethod.DIRECT_HTTP)
    repair.enter_on = [ScrapeErrorCode.HTTP_404.value]

    selected = resolve_method_candidate(
        [current, browser, repair],
        current_method_id=current.id,
        outcome=ScrapeErrorCode.HTTP_404,
    )

    assert selected is not None
    assert selected.method is repair
