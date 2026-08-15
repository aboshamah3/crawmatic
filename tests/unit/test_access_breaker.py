"""`app_shared/access/breaker.py` unit tests (audit 2026-08-15 risk H3).

Covers each of the four trip conditions independently, the minimum-sample
guards that stop a quiet window extrapolating into a spurious trip, and
the hot-path gate's behaviour when the durable store is unreadable (deny
paid work -- with Redis blind AND Postgres unreadable there is no
surviving accounting anywhere).

Pure/faked throughout; the SQL in `collect_observation` is exercised by
the integration suite, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app_shared.access.breaker import (
    BreakerObservation,
    BreakerThresholds,
    evaluate_thresholds,
    paid_requests_allowed,
    reset_gate_cache,
)
from app_shared.models.proxy_breaker import ProxyBreakerState, ProxyBreakerTrip

#: Mid-month so velocity extrapolation has real remaining time to work
#: with (15 days elapsed, ~16 remaining).
_NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
_MONTH_START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)


def _observation(**overrides: object) -> BreakerObservation:
    base = {"now": _NOW, "month_started_at": _MONTH_START}
    base.update(overrides)
    return BreakerObservation(**base)  # type: ignore[arg-type]


def _thresholds(**overrides: object) -> BreakerThresholds:
    base: dict[str, object] = {
        "monthly_proxied_requests": 100_000,
        "velocity_factor": 1.5,
        "velocity_min_sample": 200,
        "max_requests_per_url": 8.0,
        "requests_per_url_min_sample": 500,
        "max_discovery_runs_per_domain_per_day": 50,
    }
    base.update(overrides)
    return BreakerThresholds(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_gate_cache() -> None:
    reset_gate_cache()
    yield
    reset_gate_cache()


# --- trip condition 1: absolute monthly spend -------------------------------


def test_does_not_trip_on_healthy_traffic() -> None:
    verdict = evaluate_thresholds(_observation(proxied_requests_month=1_000), _thresholds())
    assert verdict.tripped is False
    assert verdict.reason is None


def test_trips_on_absolute_monthly_ceiling() -> None:
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=100_000), _thresholds()
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.MONTHLY_SPEND


def test_monthly_ceiling_none_disables_that_condition() -> None:
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=10_000_000),
        _thresholds(monthly_proxied_requests=None),
    )
    assert verdict.tripped is False


# --- trip conditions 2/3: velocity ------------------------------------------


def test_trips_on_1h_velocity_extrapolated_to_month_end() -> None:
    """500/h for the ~16 days left is ~192k, far over 100k x 1.5."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=5_000, proxied_requests_1h=500),
        _thresholds(),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.VELOCITY_1H
    assert "1h velocity" in (verdict.detail or "")


def test_trips_on_24h_velocity_when_1h_is_quiet() -> None:
    """A burst that has already passed still shows in the 24h window."""
    verdict = evaluate_thresholds(
        _observation(
            proxied_requests_month=5_000,
            proxied_requests_1h=0,
            proxied_requests_24h=20_000,
        ),
        _thresholds(),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.VELOCITY_24H


def test_velocity_min_sample_prevents_a_tiny_window_tripping() -> None:
    """3 requests in an hour must not extrapolate into a trip."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=5_000, proxied_requests_1h=3),
        _thresholds(velocity_min_sample=200),
    )
    assert verdict.tripped is False


def test_velocity_does_not_trip_on_sustainable_rate() -> None:
    """~50/h over the remaining ~16 days is ~19k on top of 5k — fine."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=5_000, proxied_requests_1h=250),
        _thresholds(monthly_proxied_requests=1_000_000),
    )
    assert verdict.tripped is False


# --- trip condition 4: proxied requests per unique URL ----------------------


def test_trips_on_requests_per_unique_url() -> None:
    """The runaway-loop signature: the same URLs fetched over and over."""
    verdict = evaluate_thresholds(
        _observation(
            proxied_requests_24h_for_ratio=10_000,
            distinct_urls_24h=100,  # 100 requests per url
        ),
        _thresholds(),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.REQUESTS_PER_URL


def test_measured_healthy_requests_per_url_does_not_trip() -> None:
    """2026-08-10 measurement: amazon 2,716 fetches / 1,097 urls = 2.48."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_24h_for_ratio=2_716, distinct_urls_24h=1_097),
        _thresholds(),
    )
    assert verdict.tripped is False


def test_requests_per_url_min_sample_prevents_a_small_batch_tripping() -> None:
    """20 requests over 1 url is a legitimate retry chain, not a loop."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_24h_for_ratio=20, distinct_urls_24h=1),
        _thresholds(requests_per_url_min_sample=500),
    )
    assert verdict.tripped is False


def test_requests_per_url_zero_distinct_urls_does_not_divide_by_zero() -> None:
    verdict = evaluate_thresholds(
        _observation(proxied_requests_24h_for_ratio=1_000, distinct_urls_24h=0),
        _thresholds(),
    )
    assert verdict.tripped is False


# --- trip condition 5: discovery runs per domain per day --------------------


def test_trips_on_discovery_runs_per_domain_per_day() -> None:
    """The 2026-08-12 hostname-normalisation rediscovery loop's shape."""
    verdict = evaluate_thresholds(
        _observation(
            max_discovery_runs_domain="www.extra.com",
            max_discovery_runs_per_domain_day=300,
        ),
        _thresholds(),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.DISCOVERY_RUNS_PER_DOMAIN
    assert "www.extra.com" in (verdict.detail or "")


def test_normal_discovery_volume_does_not_trip() -> None:
    verdict = evaluate_thresholds(
        _observation(
            max_discovery_runs_domain="www.amazon.sa",
            max_discovery_runs_per_domain_day=4,
        ),
        _thresholds(),
    )
    assert verdict.tripped is False


def test_discovery_threshold_none_disables_that_condition() -> None:
    verdict = evaluate_thresholds(
        _observation(max_discovery_runs_per_domain_day=100_000),
        _thresholds(max_discovery_runs_per_domain_per_day=None),
    )
    assert verdict.tripped is False


# --- priority ---------------------------------------------------------------


def test_absolute_overrun_is_reported_ahead_of_a_forecast() -> None:
    """The recorded reason should be the strongest true statement."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_month=200_000, proxied_requests_1h=5_000),
        _thresholds(),
    )
    assert verdict.reason is ProxyBreakerTrip.MONTHLY_SPEND


# --- hot-path gate ----------------------------------------------------------


class _FakeSession:
    def __init__(self, row: object) -> None:
        self._row = row

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kw: object) -> "_FakeSession":
        return self

    def first(self) -> object:
        return self._row


def _factory(row: object) -> object:
    return lambda: _FakeSession(row)


class _BrokenSessionFactory:
    def __call__(self) -> object:
        raise ConnectionError("postgres unavailable")


def test_gate_allows_when_breaker_is_closed() -> None:
    allowed, reason = paid_requests_allowed(
        _factory((ProxyBreakerState.CLOSED, None, None)), cache_seconds=0
    )
    assert allowed is True
    assert reason is None


def test_gate_allows_when_no_row_exists_yet() -> None:
    """Never evaluated = nothing has tripped."""
    allowed, _ = paid_requests_allowed(_factory(None), cache_seconds=0)
    assert allowed is True


def test_gate_denies_when_breaker_is_open() -> None:
    allowed, reason = paid_requests_allowed(
        _factory(
            (ProxyBreakerState.OPEN, ProxyBreakerTrip.REQUESTS_PER_URL, "100.00/url")
        ),
        cache_seconds=0,
    )
    assert allowed is False
    assert "OPEN" in (reason or "")


def test_gate_denies_paid_work_when_durable_state_is_unreadable() -> None:
    """Redis blind AND Postgres unreadable = no accounting anywhere."""
    allowed, reason = paid_requests_allowed(_BrokenSessionFactory(), cache_seconds=0)
    assert allowed is False
    assert "unreadable" in (reason or "")


def test_unreadable_state_is_not_cached() -> None:
    """A transient DB blip must not pin the fleet closed after recovery."""
    allowed, _ = paid_requests_allowed(_BrokenSessionFactory(), cache_seconds=3600)
    assert allowed is False
    # Same cache window, but the healthy read must be consulted, not the
    # previous failure.
    allowed, _ = paid_requests_allowed(
        _factory((ProxyBreakerState.CLOSED, None, None)), cache_seconds=3600
    )
    assert allowed is True


def test_gate_caches_within_the_window() -> None:
    clock = iter([100.0, 100.5, 101.0])
    factory_calls: list[int] = []

    def counting_factory() -> object:
        factory_calls.append(1)
        return _FakeSession((ProxyBreakerState.CLOSED, None, None))

    paid_requests_allowed(
        counting_factory, cache_seconds=30, monotonic=lambda: next(clock)
    )
    paid_requests_allowed(
        counting_factory, cache_seconds=30, monotonic=lambda: next(clock)
    )
    assert len(factory_calls) == 1
