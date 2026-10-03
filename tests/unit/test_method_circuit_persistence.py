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


# --- 2026-09-29 (plan E9.2): a rung that is mostly BLOCKED is demoted ------
#
# amazon.sa PROXY_HTTP over 24 h: BLOCKED 677, OK 251, TIMEOUT 192. The
# breaker never opened: any success in a flush batch reset the streak and
# RETURNED before the failure rate was looked at, and the rate itself was
# lifetime (diluted by months of history) against an 80% bar. So the ladder
# kept buying a rung that failed three times in four.


def _method() -> DomainStrategyMethod:
    return DomainStrategyMethod(
        workspace_id=uuid.uuid4(),
        domain_strategy_profile_id=uuid.uuid4(),
        access_method=AccessMethod.PROXY_HTTP,
        priority=0,
        proof_state=StrategyMethodProofState.PROVEN,
        circuit_attempt_count=50_000,
        circuit_failure_count=5_000,  # a healthy lifetime history
        consecutive_failure_count=0,
        proof_sample_size=0,
    )


_BLOCK_SETTINGS = SimpleNamespace(
    STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES=3,
    STRATEGY_METHOD_BREAKER_MIN_ATTEMPTS=10,
    STRATEGY_METHOD_BREAKER_FAILURE_RATE=0.8,
    STRATEGY_METHOD_BREAKER_COOLDOWN_SECONDS=1800,
    STRATEGY_METHOD_BREAKER_CANARY_INTERVAL_SECONDS=600,
    STRATEGY_METHOD_BLOCKED_RATE_THRESHOLD=0.5,
    STRATEGY_METHOD_BLOCKED_MIN_ATTEMPTS=20,
)


def _blocked_delta(*, success: int, blocked: int, other_failure: int = 0) -> DrainedDelta:
    failures = blocked + other_failure
    return DrainedDelta(
        attempt=success + failures,
        success=success,
        failure=failures,
        rt_ms_sum=0,
        conf_sum=0,
        qualifying_success=success,
        distinct_urls=success,
        operational_failure=failures,
        blocked=blocked,
    )


def test_a_batch_mostly_blocked_quarantines_the_rung_despite_some_successes(caplog) -> None:
    method = _method()
    now = datetime(2026, 9, 29, 2, tzinfo=timezone.utc)
    with caplog.at_level("WARNING"):
        _update_method_circuit(
            method, _blocked_delta(success=251, blocked=677, other_failure=192), _BLOCK_SETTINGS, now
        )
    assert method.proof_state is StrategyMethodProofState.QUARANTINED
    assert method.cooldown_until is not None and method.next_canary_at is not None
    reasons = [r.getMessage() for r in caplog.records if "blocked_rate" in r.getMessage()]
    assert reasons and "0.60" in reasons[0], reasons  # 677 / 1120


def test_a_healthy_batch_with_some_blocks_is_left_alone() -> None:
    method = _method()
    _update_method_circuit(
        method,
        _blocked_delta(success=80, blocked=20),
        _BLOCK_SETTINGS,
        datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    assert method.proof_state is StrategyMethodProofState.PROVEN


def test_too_few_attempts_is_no_evidence() -> None:
    method = _method()
    _update_method_circuit(
        method,
        _blocked_delta(success=1, blocked=9),
        _BLOCK_SETTINGS,
        datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    assert method.proof_state is StrategyMethodProofState.PROVEN


def test_a_single_canary_success_still_reopens_a_quarantined_rung() -> None:
    method = _method()
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    _update_method_circuit(method, _blocked_delta(success=5, blocked=45), _BLOCK_SETTINGS, now)
    assert method.proof_state is StrategyMethodProofState.QUARANTINED
    _update_method_circuit(method, _blocked_delta(success=1, blocked=0), _BLOCK_SETTINGS, now)
    assert method.proof_state is StrategyMethodProofState.PROVEN


def test_the_blocked_counter_is_buffered_and_drained() -> None:
    """The per-attempt BLOCKED flag must survive the Redis buffer."""
    from app_shared.strategy import stats_buffer

    class FakeRedis:
        def __init__(self) -> None:
            self.h: dict[str, dict[str, int]] = {}

        def hincrby(self, key, field, n):  # noqa: ANN001
            self.h.setdefault(key, {})
            self.h[key][field] = self.h[key].get(field, 0) + n

        def sadd(self, *a):  # noqa: ANN002
            pass

        def pexpire(self, *a):  # noqa: ANN002
            pass

        def hgetall(self, key):  # noqa: ANN001
            return dict(self.h.get(key, {}))

        def scard(self, key):  # noqa: ANN001
            return 0

    redis = FakeRedis()
    kwargs = dict(
        workspace_id=uuid.uuid4(), profile_id=uuid.uuid4(), method_type="ACCESS",
        method_name="PROXY_HTTP", response_time_ms=None, confidence=None, url="u",
        qualifying=False, ttl_seconds=60,
    )
    stats_buffer.record_attempt(redis, success=False, operational_failure=True, blocked=True, **kwargs)
    stats_buffer.record_attempt(redis, success=True, **kwargs)
    pending = stats_buffer.read_pending(
        redis, profile_id=kwargs["profile_id"], method_type="ACCESS", method_name="PROXY_HTTP"
    )
    assert pending.blocked == 1 and pending.attempt == 2
