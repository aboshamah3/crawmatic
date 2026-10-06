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

from datetime import UTC, datetime, timedelta

import pytest

from app_shared.access.breaker import (
    BreakerObservation,
    BreakerThresholds,
    busy_hours_horizon_seconds,
    evaluate_and_persist,
    evaluate_thresholds,
    hourly_p95,
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


def test_trips_on_1h_velocity_that_would_breach_within_a_day() -> None:
    """7,000/h all day (24 busy hours) held for one day is 168k on top of
    5k -- over 100k x 1.5. The hourly ceiling is switched off so the
    forecast condition is what is measured here."""
    verdict = evaluate_thresholds(
        _observation(
            proxied_requests_month=5_000,
            proxied_requests_1h=7_000,
            proxied_requests_24h=7_000 * 24,
        ),
        _thresholds(hourly_ceiling_floor=None),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.VELOCITY_1H
    assert "1h velocity" in (verdict.detail or "")


def test_one_hour_burst_is_not_extrapolated_over_the_whole_month() -> None:
    """The 2026-10-04 21:21Z false trip, replayed.

    A nightly refresh sends its proxied requests in a burst. The old rule
    projected that one hour over the ~27 days left in the month (700/h ->
    ~455k) against 250k x 1.5 and tripped; real month-to-date spend was
    4,632 requests (~$0.59). The burst is now held for one day (the
    workload's cycle) and the rest of the month runs at the trailing
    week's average.
    """
    now = datetime(2026, 10, 4, 21, 21, 33, tzinfo=UTC)
    verdict = evaluate_thresholds(
        BreakerObservation(
            now=now,
            month_started_at=datetime(2026, 10, 1, tzinfo=UTC),
            proxied_requests_month=4_400,
            proxied_requests_1h=700,
            proxied_requests_24h=1_900,
            proxied_requests_7d=9_000,
            proxied_requests_24h_for_ratio=1_900,
            distinct_urls_24h=1_830,
        ),
        _thresholds(monthly_proxied_requests=250_000),
    )
    assert verdict.tripped is False


def test_a_sustained_runaway_still_trips_on_the_24h_window() -> None:
    """A loop at ~3,500 proxied/h for ten hours, on top of a normal week."""
    now = datetime(2026, 10, 4, 21, 0, 0, tzinfo=UTC)
    verdict = evaluate_thresholds(
        BreakerObservation(
            now=now,
            month_started_at=datetime(2026, 10, 1, tzinfo=UTC),
            proxied_requests_month=4_400 + 35_000,
            proxied_requests_1h=3_500,
            proxied_requests_24h=2_000 + 35_000,
            proxied_requests_7d=9_000 + 35_000,
        ),
        _thresholds(monthly_proxied_requests=250_000),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.VELOCITY_24H


def test_an_inconsistent_week_count_cannot_hide_the_last_day() -> None:
    """The baseline never reads below the 24h window it contains."""
    verdict = evaluate_thresholds(
        _observation(
            proxied_requests_month=5_000,
            proxied_requests_24h=20_000,
            proxied_requests_7d=0,
        ),
        _thresholds(),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.VELOCITY_24H


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


# --- E4 (2026-10-06): absolute hourly ceiling + busy-hours horizon ----------

#: One single-tenant night of 2026-10-04, by hour: the ~700-request burst
#: hour, its tail, then nothing (24h = 1,900, matching the replay above).
_ONE_TENANT_NIGHT = [700, 600, 300, 300]


def _week_of(night: list[int]) -> list[int]:
    """Seven identical nights as hourly counts (zero hours omitted, the
    shape the hourly GROUP BY returns)."""
    return [count for _ in range(7) for count in night]


def test_a_3500_per_hour_loop_trips_on_the_first_evaluation_after_one_hour() -> None:
    """E4: a 3,500/h runaway on top of one tenant's normal week.

    The forecast holds the hour only for the measured busy hours of a day,
    so it cannot see one hour of loop (~84k by month end, far under 375k).
    The absolute ceiling -- max(3,000, 3 x the week's p95 hour) -- does,
    on the first evaluation whose trailing hour holds the loop."""
    now = datetime(2026, 10, 4, 21, 0, 0, tzinfo=UTC)
    verdict = evaluate_thresholds(
        BreakerObservation(
            now=now,
            month_started_at=datetime(2026, 10, 1, tzinfo=UTC),
            proxied_requests_month=4_400 + 3_500,
            proxied_requests_1h=3_500,
            proxied_requests_24h=1_900 + 3_500,
            proxied_requests_7d=9_000 + 3_500,
            proxied_requests_7d_p95_hourly=hourly_p95(_week_of(_ONE_TENANT_NIGHT)),
        ),
        _thresholds(monthly_proxied_requests=250_000),
    )
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.HOURLY_CEILING
    assert "3500" in (verdict.detail or "")
    assert "3000" in (verdict.detail or "")


def test_the_10_04_burst_hour_does_not_trip_at_ten_mushtryati_sized_tenants() -> None:
    """E4: the 2026-10-04 replay with ten tenants refreshing in one window.

    Every count is 10x the single-tenant replay, including the budget (a
    fleet of ten is budgeted for ten). The burst hour is 7,000 -- over the
    3,000 floor -- but every night of the week had one, so the ceiling is
    3 x that week's p95 hour and a normal night stays under it."""
    now = datetime(2026, 10, 4, 21, 21, 33, tzinfo=UTC)
    week = _week_of([10 * count for count in _ONE_TENANT_NIGHT])
    verdict = evaluate_thresholds(
        BreakerObservation(
            now=now,
            month_started_at=datetime(2026, 10, 1, tzinfo=UTC),
            proxied_requests_month=44_000,
            proxied_requests_1h=7_000,
            proxied_requests_24h=19_000,
            proxied_requests_7d=90_000,
            proxied_requests_24h_for_ratio=19_000,
            distinct_urls_24h=18_300,
            proxied_requests_7d_p95_hourly=hourly_p95(week),
        ),
        _thresholds(monthly_proxied_requests=2_500_000),
    )
    assert verdict.tripped is False


def test_hourly_ceiling_has_a_floor_when_the_week_was_quiet() -> None:
    """No history (new month of a new fleet): the floor alone applies."""
    verdict = evaluate_thresholds(
        _observation(proxied_requests_1h=2_999, proxied_requests_24h=2_999),
        _thresholds(monthly_proxied_requests=None),
    )
    assert verdict.tripped is False
    verdict = evaluate_thresholds(
        _observation(proxied_requests_1h=3_001, proxied_requests_24h=3_001),
        _thresholds(monthly_proxied_requests=None),
    )
    assert verdict.reason is ProxyBreakerTrip.HOURLY_CEILING


def test_a_fixed_hourly_ceiling_overrides_the_measured_one() -> None:
    verdict = evaluate_thresholds(
        _observation(proxied_requests_1h=1_500, proxied_requests_7d_p95_hourly=5_000),
        _thresholds(hourly_ceiling=1_000),
    )
    assert verdict.reason is ProxyBreakerTrip.HOURLY_CEILING


def test_hourly_ceiling_floor_none_disables_the_condition() -> None:
    verdict = evaluate_thresholds(
        _observation(proxied_requests_1h=50_000),
        _thresholds(monthly_proxied_requests=None, hourly_ceiling_floor=None),
    )
    assert verdict.tripped is False


def test_hourly_p95_counts_the_quiet_hours_of_the_week() -> None:
    """168 hourly buckets; hours absent from the GROUP BY are zeros."""
    assert hourly_p95([]) == 0
    # 8 busy hours of 168 sit above the 95th percentile (nearest rank 160).
    assert hourly_p95([1_000] * 8) == 0
    assert hourly_p95([1_000] * 9) == 1_000
    assert hourly_p95([100] * 160 + [5_000] * 8) == 100


@pytest.mark.parametrize(
    ("count_1h", "count_24h", "hours"),
    [
        (700, 1_900, 1_900 / 700),  # the 10-04 night: ~2.7 busy hours
        (700, 0, 1.0),  # an inconsistent 24h never shortens below 1h
        (700, 700, 1.0),
        (100, 100 * 30, 24.0),  # clamped to a day
        (0, 5_000, 24.0),  # a quiet last hour: the whole day was busy
    ],
)
def test_the_1h_horizon_is_the_days_measured_busy_hours(
    count_1h: int, count_24h: int, hours: float
) -> None:
    assert busy_hours_horizon_seconds(count_1h, count_24h) == pytest.approx(hours * 3600)


def test_the_configured_1h_horizon_caps_the_busy_hours() -> None:
    assert busy_hours_horizon_seconds(100, 2_400, cap_seconds=3 * 3600) == 3 * 3600


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


# --- cooldown auto-recovery (EPA B1, 2026-09-03) -----------------------------
#
# Before B1 an OPEN breaker was a permanent state: `evaluate_and_persist`
# only ever tripped, and the only reset was an operator running
# `close_breaker` by hand. Combined with the durable evaluator cadence
# (which now keeps evidence fresh whether or not anything is scraping),
# that made a single trip an indefinite full stop on paid work even after
# the condition that caused it had been gone for days. Auto-recovery is
# strictly bounded: a cooldown must have fully elapsed AND the *current*
# window must be passing, so re-arming a live runaway remains impossible.


class _FakeBreakerRow:
    """The `proxy_circuit_breakers` row as a plain object.

    Only the columns the evaluator reads or writes; assertions below read
    the same attributes the real ORM row would carry, so a change that
    stops persisting one of them fails here.
    """

    def __init__(
        self,
        *,
        state: ProxyBreakerState = ProxyBreakerState.CLOSED,
        tripped_at: datetime | None = None,
        detail: str | None = None,
    ) -> None:
        self.id = "breaker-row"
        self.scope_key = "global"
        self.state = state
        self.trip_reason = ProxyBreakerTrip.MONTHLY_SPEND if detail else None
        self.detail = detail
        self.observed: dict | None = None
        self.tripped_at = tripped_at
        self.cleared_at: datetime | None = None
        self.trip_count = 1 if state is ProxyBreakerState.OPEN else 0


class _EvalResult:
    """One `session.execute(...)` result.

    Dispatches per ACCESSOR rather than per statement, because that is what
    actually distinguishes the calls `evaluate_and_persist` makes: the row
    lookup uses `scalar_one_or_none`, the lease UPDATE and the discovery
    aggregate use `first`, the two count aggregates use `scalar_one`, the
    24h count/distinct pair uses `one`, and the per-hour week uses `all`.
    """

    def __init__(self, session: "_EvalSession") -> None:
        self._session = session

    def scalar_one_or_none(self) -> object:
        return self._session.row

    def scalar_one(self) -> object:
        return self._session.scalars.pop(0)

    def one(self) -> object:
        return self._session.day_counts

    def first(self) -> object:
        return self._session.firsts.pop(0)

    def all(self) -> list[object]:
        """The trailing week's per-hour counts (E4 hourly ceiling)."""
        return self._session.hourly


class _EvalSession:
    def __init__(
        self,
        row: _FakeBreakerRow,
        *,
        claimed: bool = True,
        month: int = 0,
        hour: int = 0,
        day: tuple[int, int] = (0, 0),
        week: int = 0,
        discovery: tuple[str, int] | None = None,
    ) -> None:
        self.row = row
        self.scalars = [month, hour, week]
        self.day_counts = day
        self.firsts = [("breaker-row",) if claimed else None, discovery]
        self.hourly: list[tuple[object, int]] = []
        self.added: list[object] = []

    def execute(self, *_args: object, **_kw: object) -> _EvalResult:
        return _EvalResult(self)

    def add(self, obj: object) -> None:  # pragma: no cover - row always exists
        self.added.append(obj)

    def flush(self) -> None:  # pragma: no cover - row always exists
        pass


def _open_row(*, opened_seconds_ago: int) -> _FakeBreakerRow:
    return _FakeBreakerRow(
        state=ProxyBreakerState.OPEN,
        tripped_at=_NOW - timedelta(seconds=opened_seconds_ago),
        detail="month-to-date proxied requests 250000 >= ceiling 250000",
    )


def test_open_breaker_auto_closes_after_cooldown_when_window_is_healthy() -> None:
    """Cooldown fully elapsed and the CURRENT window passes every threshold
    -> the breaker closes itself and says why."""
    row = _open_row(opened_seconds_ago=3601)
    session = _EvalSession(row)

    verdict = evaluate_and_persist(
        session,
        thresholds=BreakerThresholds(),
        min_interval_seconds=0,
        now=_NOW,
        auto_close_after_seconds=3600,
    )

    assert verdict is not None
    assert verdict.tripped is False
    assert verdict.state is ProxyBreakerState.CLOSED
    assert row.state is ProxyBreakerState.CLOSED
    assert row.cleared_at == _NOW
    # The reason is durable, not just logged: an operator reading the row
    # must be able to tell an auto-recovery from a manual reset.
    assert "auto-recovery" in (row.detail or ""), row.detail


def test_open_breaker_stays_open_inside_cooldown() -> None:
    """A healthy window is not enough on its own. The cooldown exists so a
    runaway that pauses for a minute cannot immediately re-arm itself."""
    row = _open_row(opened_seconds_ago=600)
    session = _EvalSession(row)

    verdict = evaluate_and_persist(
        session,
        thresholds=BreakerThresholds(),
        min_interval_seconds=0,
        now=_NOW,
        auto_close_after_seconds=3600,
    )

    assert verdict is not None
    assert verdict.state is ProxyBreakerState.OPEN
    assert row.state is ProxyBreakerState.OPEN
    assert row.cleared_at is None


def test_auto_close_disabled_never_closes_however_long_it_has_been_open() -> None:
    """`auto_close_after_seconds=0` is the documented operator-only posture,
    and it is also the DEFAULT — no caller gets auto-recovery by accident."""
    row = _open_row(opened_seconds_ago=30 * 86400)
    session = _EvalSession(row)

    verdict = evaluate_and_persist(
        session,
        thresholds=BreakerThresholds(),
        min_interval_seconds=0,
        now=_NOW,
    )

    assert verdict is not None
    assert verdict.state is ProxyBreakerState.OPEN
    assert row.state is ProxyBreakerState.OPEN
    assert row.cleared_at is None


def test_open_breaker_past_cooldown_with_an_unhealthy_window_stays_open() -> None:
    """THE safety property. Cooldown elapsed, but the spend that tripped it
    is still happening -> re-arming would restart the runaway the breaker
    was built to stop."""
    row = _open_row(opened_seconds_ago=86400)
    session = _EvalSession(row, month=250_000)

    verdict = evaluate_and_persist(
        session,
        thresholds=BreakerThresholds(monthly_proxied_requests=100_000),
        min_interval_seconds=0,
        now=_NOW,
        auto_close_after_seconds=3600,
    )

    assert verdict is not None
    assert verdict.tripped is True
    assert verdict.reason is ProxyBreakerTrip.MONTHLY_SPEND
    assert verdict.state is ProxyBreakerState.OPEN
    assert row.state is ProxyBreakerState.OPEN
    assert row.cleared_at is None


def test_auto_close_never_runs_without_the_evaluator_lease() -> None:
    """Recovery is a real evaluation, so it obeys the same lease every other
    verdict does — N processes cannot each decide to close the breaker."""
    row = _open_row(opened_seconds_ago=86400)
    session = _EvalSession(row, claimed=False)

    verdict = evaluate_and_persist(
        session,
        thresholds=BreakerThresholds(),
        min_interval_seconds=300,
        now=_NOW,
        auto_close_after_seconds=3600,
    )

    assert verdict is None
    assert row.state is ProxyBreakerState.OPEN
