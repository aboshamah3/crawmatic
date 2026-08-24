from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app_shared.enums import AccessMethod, StrategyMethodProofState
from app_shared.models.strategy import DomainStrategyMethod
from app_shared.strategy.flush import _update_method_circuit
from app_shared.strategy.stats_buffer import DrainedDelta


def _delta(*, success: int = 0, operational_failure: int = 0) -> DrainedDelta:
    return DrainedDelta(
        attempt=success + operational_failure,
        success=success,
        failure=operational_failure,
        rt_ms_sum=0,
        conf_sum=0,
        qualifying_success=success,
        distinct_urls=success,
        operational_failure=operational_failure,
    )


def test_method_scoped_breaker_quarantines_and_canary_success_recovers() -> None:
    method = DomainStrategyMethod(
        workspace_id=uuid.uuid4(),
        domain_strategy_profile_id=uuid.uuid4(),
        access_method=AccessMethod.DIRECT_HTTP,
        priority=0,
        proof_state=StrategyMethodProofState.PROVEN,
        circuit_attempt_count=0,
        circuit_failure_count=0,
        consecutive_failure_count=0,
        proof_sample_size=0,
    )
    settings = SimpleNamespace(
        STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES=3,
        STRATEGY_METHOD_BREAKER_MIN_ATTEMPTS=10,
        STRATEGY_METHOD_BREAKER_FAILURE_RATE=0.8,
        STRATEGY_METHOD_BREAKER_COOLDOWN_SECONDS=1800,
        STRATEGY_METHOD_BREAKER_CANARY_INTERVAL_SECONDS=600,
    )
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)

    for _ in range(3):
        _update_method_circuit(method, _delta(operational_failure=1), settings, now)

    assert method.proof_state is StrategyMethodProofState.QUARANTINED
    assert method.consecutive_failure_count == 3
    assert method.next_canary_at is not None

    _update_method_circuit(method, _delta(success=1), settings, now)

    assert method.proof_state is StrategyMethodProofState.PROVEN
    assert method.consecutive_failure_count == 0
    assert method.cooldown_until is None
    assert method.next_canary_at is None


def test_catalog_outcome_does_not_change_method_health() -> None:
    method = DomainStrategyMethod(
        workspace_id=uuid.uuid4(),
        domain_strategy_profile_id=uuid.uuid4(),
        access_method=AccessMethod.DIRECT_HTTP,
        priority=0,
        proof_state=StrategyMethodProofState.PROVEN,
        circuit_attempt_count=0,
        circuit_failure_count=0,
        consecutive_failure_count=0,
    )
    _update_method_circuit(
        method,
        _delta(),
        SimpleNamespace(),
        datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert method.circuit_attempt_count == 0
    assert method.proof_state is StrategyMethodProofState.PROVEN
