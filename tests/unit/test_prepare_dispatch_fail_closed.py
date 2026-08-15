"""`_prepare_dispatch` fail-closed-for-paid-work tests (audit 2026-08-15, H3).

This is the test that matters most for H3, because the failure mode this
change guards against has two ways to get it wrong and only one right
answer:

* deny too much -> a Redis outage stops ALL scraping, including the free
  direct traffic that is the majority of the catalog;
* deny too little -> the cost hole stays open and a Redis incident plus a
  retry/rediscovery loop spends money with no ceiling.

So every test below asserts BOTH halves of the split against the same
simulated Redis outage: a proxied plan must be denied, a direct plan must
proceed untouched.

`plan.use_proxy` is the discriminator under test -- it is what decides
whether `assign_proxy` runs, whether the monthly budget counter is
incremented, and whether the request is routed through DataImpulse.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.enums import AccessMethod, AccessStrategy, RobotsPolicy, ScrapeErrorCode
from app_shared.models.access import AccessPolicy

from scrape_core import targets as targets_mod
from scrape_core.targets import SpiderTarget, _prepare_dispatch


class _BrokenRedis:
    """Every command raises — a total Redis outage."""

    def incr(self, *_a: object, **_kw: object) -> int:
        raise ConnectionError("redis unavailable")

    def expire(self, *_a: object, **_kw: object) -> None:
        raise ConnectionError("redis unavailable")

    def ttl(self, *_a: object, **_kw: object) -> int:
        raise ConnectionError("redis unavailable")

    def set(self, *_a: object, **_kw: object) -> bool:
        raise ConnectionError("redis unavailable")


class _HealthyRedis:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> None:
        return None

    def ttl(self, key: str) -> int:
        return -2

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _policy(strategy: AccessStrategy, **overrides: object) -> AccessPolicy:
    """A minimally-populated AccessPolicy (never persisted)."""
    policy = AccessPolicy(
        id=uuid.uuid4(),
        name="test-policy",
        strategy=strategy,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
        max_requests_per_minute=60,
        max_requests_per_hour=None,
        max_requests_per_day=None,
        rotate_per_request=False,
        sticky_session=False,
        provider_id=None,
        country_code=None,
    )
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def _target(policy: AccessPolicy) -> SpiderTarget:
    return SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        domain="shop.example.com",
        access_policy=policy,
    )


@pytest.fixture
def outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Redis being completely down, breaker enabled and closed."""
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: _BrokenRedis())
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", lambda: (True, None))


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: _HealthyRedis())
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", lambda: (True, None))


def _set_fail_open(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    class _S:
        PROXY_LEDGER_FAIL_OPEN = value

    monkeypatch.setattr(targets_mod, "_settings", lambda: _S())


# --- Redis down: DIRECT work continues --------------------------------------


def test_redis_outage_lets_a_direct_attempt_proceed(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_ONLY never proxies, so an outage must not stop it at all."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_ONLY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False
    assert decision.proxy is None


def test_redis_outage_lets_the_direct_step_of_a_mixed_strategy_proceed(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_THEN_PROXY attempt 1 is direct — unaffected by the outage."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_THEN_PROXY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False


# --- Redis down: PROXIED work is denied -------------------------------------


def test_redis_outage_degrades_a_proxied_retry_to_direct(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_THEN_PROXY retry wants a proxy; with no ledger it must not
    get one — but the strategy has a direct step, so scraping continues
    unpaid rather than stopping."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_THEN_PROXY))

    decision = _prepare_dispatch(target, 2, {}, {})

    assert decision.plan is not None
    assert decision.plan.use_proxy is False, "paid retry must not be authorised"
    assert decision.plan.access_method is AccessMethod.DIRECT_HTTP_RETRY
    assert decision.proxy is None


def test_redis_outage_stops_a_proxy_only_strategy_with_limit_reached(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROXY_FIRST has no direct step to fall back to, so the target is
    skipped cleanly (a terminal, finalizable outcome) instead of being
    fetched through a paid proxy with no accounting."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.PROXY_FIRST))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.plan is None
    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED
    assert decision.attempted_method is AccessMethod.PROXY_HTTP


def test_redis_outage_stops_residential_only(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.RESIDENTIAL_ONLY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED


def test_paid_denial_never_assigns_a_proxy(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`assign_proxy` must not even be reached — no upstream session is
    opened for a request we have refused to pay for."""
    called: list[object] = []
    monkeypatch.setattr(
        targets_mod, "assign_proxy", lambda **kw: called.append(kw) or None
    )
    _set_fail_open(monkeypatch, False)

    _prepare_dispatch(_target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {})

    assert called == []


# --- emergency override ------------------------------------------------------


def test_emergency_override_restores_fail_open_for_paid_work(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROXY_LEDGER_FAIL_OPEN=true is the incident escape hatch."""
    _set_fail_open(monkeypatch, True)
    target = _target(_policy(AccessStrategy.PROXY_FIRST))

    decision = _prepare_dispatch(target, 1, {}, {})

    # The plan stays proxied; it only stops later for want of an eligible
    # provider (none configured in this fixture), not for want of a ledger.
    assert decision.skip_error_code is ScrapeErrorCode.PROXY_FAILED


# --- circuit breaker gate ----------------------------------------------------


def test_open_breaker_denies_paid_work_even_with_healthy_redis(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The breaker is independent of Redis: a healthy ledger that says
    'allowed' does not override a tripped durable breaker."""
    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(
        targets_mod,
        "_breaker_allows_paid_work",
        lambda: (False, "circuit breaker OPEN (REQUESTS_PER_URL): 100.00/url"),
    )

    decision = _prepare_dispatch(_target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {})

    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED


def test_open_breaker_does_not_stop_direct_work(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tripped spend breaker stops PAID work only — free direct
    scraping keeps the catalog fresh while an operator investigates."""
    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(
        targets_mod, "_breaker_allows_paid_work", lambda: (False, "circuit breaker OPEN")
    )

    decision = _prepare_dispatch(_target(_policy(AccessStrategy.DIRECT_ONLY)), 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False


def test_breaker_is_not_consulted_for_a_direct_plan(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No database round trip on the free path."""
    calls: list[int] = []

    def _counting() -> tuple[bool, str | None]:
        calls.append(1)
        return True, None

    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", _counting)

    _prepare_dispatch(_target(_policy(AccessStrategy.DIRECT_ONLY)), 1, {}, {})

    assert calls == []
