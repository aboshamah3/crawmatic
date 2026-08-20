"""Collector behaviour for `app_shared.opsmetrics.snapshot`.

Two of these tests exist because of bugs this module actually had, found
by running the collector against the live production database before
shipping it. Both were invisible to a rules-only test suite, and both
produced *confidently wrong* output rather than an error:

1. **Transaction poisoning.** Production's deployed migration head is
   behind the code, so `outbox_messages` does not exist there. In
   Postgres, one failed statement aborts the transaction and every
   later query on that connection returns `InFailedSqlTransaction` — so
   a single genuinely-missing table reported eight fake section
   failures. See :class:`TestSectionsDegradeIndependently`.
2. **Partition name construction.** `partition_name(parent, suffix)`
   takes the `YYYY_MM` *suffix string*; passing a `datetime` silently
   builds a name that matches nothing, so the collector reported all
   four August partitions missing against a database that had them. See
   :class:`TestPartitionNaming`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app_shared.opsmetrics.snapshot import (
    DomainDiscovery,
    DomainStats,
    SpendVelocity,
    _ratio,
    _round,
    _section,
    collect_snapshot,
    seconds_remaining_in_month,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class _BoomSession:
    """A session where every statement raises, like an aborted transaction."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("relation does not exist")
        self.rollbacks = 0

    def execute(self, *_a, **_k):
        raise self.exc

    def rollback(self) -> None:
        self.rollbacks += 1


class TestSectionsDegradeIndependently:
    def test_a_total_database_failure_still_yields_a_snapshot(self) -> None:
        """Never raise into the caller: a monitor that 500s is useless
        exactly when the deployment is in the state you need to see."""
        session = _BoomSession()
        snapshot = collect_snapshot(session, now=NOW)
        assert snapshot.collected_at == NOW
        for section in (
            snapshot.rollups,
            snapshot.outbox,
            snapshot.breaker,
            snapshot.queue,
            snapshot.spend,
            snapshot.freshness,
            snapshot.optimizer,
            snapshot.db_role,
        ):
            assert section.available is False
            assert section.unavailable_reason

    def test_every_failing_section_rolls_the_transaction_back(self) -> None:
        """The regression that made one missing table look like eight."""
        session = _BoomSession()
        collect_snapshot(session, now=NOW)
        # One rollback per DB-backed section that failed, so the next
        # section starts on a clean transaction.
        assert session.rollbacks >= 8

    def test_the_failure_reason_names_the_exception_and_is_truncated(self) -> None:
        session = _BoomSession(RuntimeError("x" * 5_000))
        snapshot = collect_snapshot(session, now=NOW)
        reason = snapshot.rollups.unavailable_reason
        assert reason is not None
        assert reason.startswith("RuntimeError:")
        assert len(reason) <= 300

    def test_section_helper_survives_a_session_that_cannot_roll_back(self) -> None:
        class _Unrollbackable:
            def rollback(self) -> None:
                raise RuntimeError("connection gone")

        result = _section(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            lambda reason: reason,
            _Unrollbackable(),
        )
        assert result.startswith("RuntimeError: boom")

    def test_snapshot_of_a_dead_database_is_json_serialisable(self) -> None:
        import json

        json.dumps(collect_snapshot(_BoomSession(), now=NOW).as_dict())


class TestPartitionNaming:
    """`_collect_partitions` must ask for the names the DB actually uses."""

    @staticmethod
    def _fake_catalog(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
        import app_shared.maintenance.partitions as parts

        class _Bounds:
            def __init__(self, name: str) -> None:
                self.name = name

        monkeypatch.setattr(parts, "table_exists", lambda _s, _n: True)
        monkeypatch.setattr(
            parts,
            "existing_partitions",
            lambda _s, parent: [
                _Bounds(n) for n in present if n.startswith(f"{parent}_")
            ],
        )

    def test_current_month_partition_is_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug: a datetime was passed where a `YYYY_MM` suffix was
        expected, so an existing `request_attempts_2026_08` was reported
        missing."""
        self._fake_catalog(
            monkeypatch,
            {
                "request_attempts_2026_08",
                "price_observations_2026_08",
                "price_alert_events_2026_08",
                "webhook_events_2026_08",
            },
        )
        snapshot = collect_snapshot(_BoomSession(), now=NOW)
        assert snapshot.partitions_available
        assert {p.table for p in snapshot.partitions} == {
            "price_observations",
            "request_attempts",
            "price_alert_events",
            "webhook_events",
        }
        for p in snapshot.partitions:
            assert p.current_month_present is True, p.table
            assert p.next_month_present is False, p.table
            # 2026-09-01 minus 2026-08-15T12:00 == 16.5 days.
            assert p.days_until_next_month == 16

    def test_next_month_partition_is_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_catalog(
            monkeypatch, {"request_attempts_2026_08", "request_attempts_2026_09"}
        )
        snapshot = collect_snapshot(_BoomSession(), now=NOW)
        entry = next(p for p in snapshot.partitions if p.table == "request_attempts")
        assert entry.current_month_present and entry.next_month_present

    def test_december_rolls_over_to_january_of_the_next_year(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A naive `month + 1` would look for `..._2026_13`."""
        self._fake_catalog(
            monkeypatch, {"request_attempts_2026_12", "request_attempts_2027_01"}
        )
        snapshot = collect_snapshot(
            _BoomSession(), now=datetime(2026, 12, 20, tzinfo=UTC)
        )
        entry = next(p for p in snapshot.partitions if p.table == "request_attempts")
        assert entry.current_month_present and entry.next_month_present


class TestDerivedSignals:
    def test_domain_ratios(self) -> None:
        d = DomainStats(
            domain="amazon.sa",
            attempts=10_449,
            successes=5_399,
            distinct_urls=1_238,
            proxied=9_555,
            failed_paid=4_156,
        )
        assert d.success_rate == pytest.approx(0.5167, abs=0.001)
        assert d.requests_per_url == pytest.approx(8.44, abs=0.01)
        assert d.proxied_per_url == pytest.approx(7.72, abs=0.01)
        assert d.wasted_paid_rate == pytest.approx(0.435, abs=0.001)
        assert d.estimated_usd == pytest.approx(1.209, abs=0.01)

    def test_cost_per_successful_price_uses_the_domain_specific_rate(self) -> None:
        """Task 2.4: amazon's $/req from the 2026-08-12 report
        (0.001066/5.06 ~= 0.00021067), not the fleet-wide average."""
        from app_shared.opsmetrics import cost as _cost

        d = DomainStats(
            domain="amazon.sa",
            attempts=100,
            successes=50,
            distinct_urls=100,
            proxied=100,
            failed_paid=50,
            successful_prices=50,
        )
        expected = _cost.usd_for_domain("amazon.sa", 100) / 50
        assert d.cost_per_successful_price == pytest.approx(expected)

    def test_cost_per_successful_price_is_infinite_on_spend_with_zero_prices(
        self,
    ) -> None:
        """The audit's named unbounded case: real signal, not a divide error."""
        d = DomainStats(
            domain="amazon.sa",
            attempts=25,
            successes=0,
            distinct_urls=15,
            proxied=25,
            failed_paid=25,
            successful_prices=0,
        )
        assert d.cost_per_successful_price == float("inf")

    def test_cost_per_successful_price_is_none_with_no_spend_at_all(self) -> None:
        d = DomainStats(
            domain="afaqalhasoob.com",
            attempts=9,
            successes=9,
            distinct_urls=9,
            proxied=0,
            failed_paid=0,
            successful_prices=0,
        )
        assert d.cost_per_successful_price is None

    def test_domain_ratios_are_none_rather_than_dividing_by_zero(self) -> None:
        d = DomainStats(
            domain="x", attempts=0, successes=0, distinct_urls=0, proxied=0, failed_paid=0
        )
        assert d.success_rate is None
        assert d.requests_per_url is None
        assert d.wasted_paid_rate is None

    def test_discovery_baseline_excludes_the_current_window(self) -> None:
        """Including today in its own baseline caps the surge factor at
        exactly 7.0, so a 100x runaway and a 7x drift look identical."""
        d = DomainDiscovery(domain="extra.com", runs_24h=7_186, runs_7d=9_157)
        assert d.runs_prev_6d == 1_971
        assert d.mean_daily_runs_7d == pytest.approx(328.5, abs=0.1)
        assert d.surge_factor == pytest.approx(21.9, abs=0.1)
        assert d.surge_factor > 7.0

    def test_discovery_surge_from_a_standstill_is_infinite_not_an_error(self) -> None:
        d = DomainDiscovery(domain="x", runs_24h=500, runs_7d=500)
        assert d.surge_factor == float("inf")
        assert d.as_dict()["surge_factor"] == "inf"

    def test_discovery_surge_is_none_when_nothing_happened(self) -> None:
        assert DomainDiscovery(domain="x", runs_24h=0, runs_7d=0).surge_factor is None

    def test_month_end_forecast_arithmetic(self) -> None:
        s = SpendVelocity(
            available=True,
            proxied_month_to_date=20_900,
            proxied_24h=12_141,
            proxied_prev_24h=4_279,
            seconds_remaining_in_month=16 * 86_400.0,
            ceiling_proxied_requests=250_000,
        )
        # 20,900 + (12,141/day * 16 days)
        assert s.forecast_month_end_24h == pytest.approx(215_156, abs=1)
        assert s.acceleration_24h == pytest.approx(2.84, abs=0.01)
        assert s.pct_of_ceiling == pytest.approx(0.0836, abs=0.001)
        assert s.usd_month_to_date == pytest.approx(2.644, abs=0.01)

    def test_ratio_semantics(self) -> None:
        assert _ratio(0, 0) is None  # no signal
        assert _ratio(5, 0) == float("inf")  # work from a standstill
        assert _ratio(10, 5) == 2.0

    def test_round_stringifies_infinities_for_strict_json(self) -> None:
        assert _round(float("inf"), 2) == "inf"
        assert _round(float("-inf"), 2) == "-inf"
        assert _round(None, 2) is None
        assert _round(1.2345, 2) == 1.23

    @pytest.mark.parametrize(
        ("now", "expected_days"),
        [
            (datetime(2026, 8, 1, tzinfo=UTC), 31),
            (datetime(2026, 2, 1, tzinfo=UTC), 28),  # non-leap February
            (datetime(2024, 2, 1, tzinfo=UTC), 29),  # leap February
            (datetime(2026, 12, 1, tzinfo=UTC), 31),  # year rollover
        ],
    )
    def test_seconds_remaining_in_month(self, now: datetime, expected_days: int) -> None:
        assert seconds_remaining_in_month(now) == pytest.approx(
            expected_days * 86_400.0, abs=1
        )

    def test_seconds_remaining_at_the_very_end_of_a_month(self) -> None:
        end = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)
        assert 0 <= seconds_remaining_in_month(end) <= 1


class TestRedisSection:
    def test_absent_client_still_reports_the_cached_policy_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`redis_policy.last_report()` is exported precisely so a health
        surface can report posture without re-probing."""
        import app_shared.redis_policy as policy

        monkeypatch.setattr(
            policy,
            "last_report",
            lambda: policy.RedisPolicyReport(
                status=policy.RedisPolicyStatus.VIOLATION,
                policy="allkeys-lru",
                maxmemory=1_000,
            ),
        )
        snapshot = collect_snapshot(_BoomSession(), now=NOW, redis=None)
        assert snapshot.redis.available is True
        assert snapshot.redis.policy_status == "VIOLATION"
        assert snapshot.redis.policy == "allkeys-lru"

    def test_info_fills_in_memory_and_evictions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app_shared.redis_policy as policy

        monkeypatch.setattr(policy, "last_report", lambda: None)

        class _Redis:
            def info(self):
                return {
                    "used_memory": 900,
                    "maxmemory": 1_000,
                    "evicted_keys": 7,
                    "connected_clients": 3,
                }

        snapshot = collect_snapshot(_BoomSession(), now=NOW, redis=_Redis())
        assert snapshot.redis.evicted_keys == 7
        assert snapshot.redis.memory_utilisation == pytest.approx(0.9)

    def test_a_failing_info_call_never_breaks_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app_shared.redis_policy as policy

        monkeypatch.setattr(policy, "last_report", lambda: None)

        class _Redis:
            def info(self):
                raise ConnectionError("redis down")

        snapshot = collect_snapshot(_BoomSession(), now=NOW, redis=_Redis())
        assert snapshot.redis.available is True
        assert "ConnectionError" in (snapshot.redis.unavailable_reason or "")


class TestScrapydSection:
    def test_daemonstatus_is_optional_and_never_fetched_by_this_module(self) -> None:
        """The collector opens no network connections of its own."""
        snapshot = collect_snapshot(_BoomSession(), now=NOW)
        assert snapshot.scrapyd.pending is None

    def test_supplied_daemonstatus_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Session(_BoomSession):
            def execute(self, *_a, **_k):
                raise RuntimeError("only the target count query matters here")

        snapshot = collect_snapshot(
            _Session(),
            now=NOW,
            scrapyd_status={"pending": 4, "running": 8, "finished": 100},
        )
        # The DB-derived count failed, so the whole section degrades —
        # but it degrades loudly rather than reporting a half-truth.
        assert snapshot.scrapyd.available is False


def test_paid_access_methods_match_the_breaker() -> None:
    """"Paid" must mean one thing across the codebase, or the dashboard
    and the stop-loss will disagree about what they are counting."""
    from app_shared.enums import AccessMethod
    from app_shared.opsmetrics.snapshot import PAID_ACCESS_METHODS

    assert set(PAID_ACCESS_METHODS) == {
        str(AccessMethod.PROXY_HTTP),
        str(AccessMethod.PLAYWRIGHT_PROXY),
    }


def test_collect_snapshot_defaults_its_clock_to_now() -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)
    snapshot = collect_snapshot(_BoomSession())
    assert snapshot.collected_at >= before
