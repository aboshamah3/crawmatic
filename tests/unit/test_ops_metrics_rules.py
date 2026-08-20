"""Unit tests for the H5 alert rules (`app_shared.opsmetrics.rules`).

The rules are pure (snapshot + thresholds -> alerts), so these tests
build snapshots directly and never touch a database.

The design test stated in the H5 brief is that each of the four silent
production failures found this cycle must produce an alert. Those four
have a dedicated test class (:class:`TestTheFourSilentFailures`) and are
the reason this file exists; everything else is supporting coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app_shared.opsmetrics import cost
from app_shared.opsmetrics.rules import (
    DEFAULT_THRESHOLDS,
    RULES,
    Category,
    Severity,
    Thresholds,
    evaluate,
    worst_severity,
)
from app_shared.opsmetrics.snapshot import (
    BreakerHealth,
    DatabaseRoleHealth,
    DomainDiscovery,
    DomainStats,
    Freshness,
    OpsSnapshot,
    OptimizerChurn,
    OutboxHealth,
    PartitionHealth,
    QueueHealth,
    RedisHealth,
    RollupHealth,
    ScrapydHealth,
    SpendVelocity,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def healthy_snapshot(**overrides) -> OpsSnapshot:
    """A snapshot where every rule passes. Tests override one field."""
    base = dict(
        collected_at=NOW,
        partitions=(
            PartitionHealth(
                table="price_observations",
                exists=True,
                current_month_present=True,
                next_month_present=True,
                partition_count=3,
                days_until_next_month=16,
            ),
        ),
        rollups=RollupHealth(
            available=True,
            rows=5_000,
            max_rollup_date=NOW.date(),
            observation_rows=32_231,
            max_observation_at=NOW,
            lag_days=0,
            unrolled_observations=0,
        ),
        outbox=OutboxHealth(available=True, pending=3, dead=0, published=900),
        breaker=BreakerHealth(
            available=True,
            scope_key="global",
            state="CLOSED",
            trip_count=0,
            seconds_since_evaluation=120.0,
        ),
        queue=QueueHealth(
            available=True,
            jobs_by_status={"COMPLETED": 10},
            targets_by_status={"COMPLETED": 100},
        ),
        domains_24h=(
            DomainStats(
                domain="pcpalace.com.sa",
                attempts=1_500,
                successes=1_475,
                distinct_urls=600,
                proxied=0,
                failed_paid=0,
            ),
        ),
        discovery=(DomainDiscovery(domain="jarir.com", runs_24h=4, runs_7d=30),),
        spend=SpendVelocity(
            available=True,
            proxied_month_to_date=20_900,
            proxied_1h=100,
            proxied_prev_1h=100,
            proxied_24h=2_400,
            proxied_prev_24h=2_400,
            seconds_remaining_in_month=16 * 86_400.0,
            ceiling_proxied_requests=250_000,
        ),
        freshness=Freshness(
            available=True,
            last_attempt_at=NOW - timedelta(minutes=5),
            seconds_since_attempt=300.0,
            refresh_rules_total=12,
            refresh_rules_enabled=12,
        ),
        optimizer=OptimizerChurn(
            available=True, profiles_total=17, changed_24h=1, changed_7d=4
        ),
        redis=RedisHealth(
            available=True,
            policy_status="COMPLIANT",
            policy="noeviction",
            used_memory=10_000,
            maxmemory_from_info=1_000_000,
            evicted_keys=0,
        ),
        scrapyd=ScrapydHealth(available=True, in_flight_targets=12),
        db_role=DatabaseRoleHealth(
            available=True,
            role="crawmatic_app",
            is_superuser=False,
            bypasses_rls=False,
        ),
    )
    base.update(overrides)
    return OpsSnapshot(**base)


def ids(alerts) -> set[str]:
    return {a.rule_id for a in alerts}


def by_id(alerts, rule_id: str):
    return [a for a in alerts if a.rule_id == rule_id]


# --------------------------------------------------------------------------
# The baseline: a healthy fleet must be silent
# --------------------------------------------------------------------------


def test_healthy_snapshot_fires_nothing() -> None:
    """An alerting system that always fires gets muted, and then you are
    back to silent failures. The all-clear case must be genuinely clear."""
    assert evaluate(healthy_snapshot()) == []
    assert worst_severity([]) is None


# --------------------------------------------------------------------------
# The four silent production failures this cycle found
# --------------------------------------------------------------------------


class TestTheFourSilentFailures:
    """The H5 design test: would this dashboard have caught them?"""

    def test_1_missing_next_month_partition_is_critical_inside_the_fuse(self) -> None:
        """No 2026_09 partition on any partitioned table => every INSERT
        fails at midnight on 2026-09-01. Went unnoticed for weeks."""
        snap = healthy_snapshot(
            partitions=(
                PartitionHealth(
                    table="request_attempts",
                    exists=True,
                    current_month_present=True,
                    next_month_present=False,
                    partition_count=2,
                    days_until_next_month=10,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "partition.next_month_missing")
        assert len(alerts) == 1
        assert alerts[0].severity is Severity.CRITICAL
        assert alerts[0].subject == "request_attempts"
        assert alerts[0].observed["days_until_next_month"] == 10

    def test_1b_missing_partition_is_high_while_the_fuse_is_long(self) -> None:
        snap = healthy_snapshot(
            partitions=(
                PartitionHealth(
                    table="request_attempts",
                    exists=True,
                    current_month_present=True,
                    next_month_present=False,
                    partition_count=2,
                    days_until_next_month=25,
                ),
            )
        )
        assert by_id(evaluate(snap), "partition.next_month_missing")[
            0
        ].severity is Severity.HIGH

    def test_1c_missing_current_month_partition_is_always_critical(self) -> None:
        snap = healthy_snapshot(
            partitions=(
                PartitionHealth(
                    table="price_observations",
                    exists=True,
                    current_month_present=False,
                    next_month_present=True,
                    partition_count=1,
                    days_until_next_month=25,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "partition.current_month_missing")
        assert alerts and alerts[0].severity is Severity.CRITICAL

    def test_1d_absent_table_is_not_reported_as_a_missing_partition(self) -> None:
        """`webhook_events` is registered ahead of its own migration; an
        absent parent is a clean skip, not a false CRITICAL."""
        snap = healthy_snapshot(
            partitions=(
                PartitionHealth(
                    table="webhook_events",
                    exists=False,
                    current_month_present=False,
                    next_month_present=False,
                    partition_count=0,
                    days_until_next_month=3,
                ),
            )
        )
        assert not ids(evaluate(snap)) & {
            "partition.next_month_missing",
            "partition.current_month_missing",
        }

    def test_2_rollup_that_never_ran_is_critical(self) -> None:
        """0 rollup rows against 32,231 observations spanning a month."""
        snap = healthy_snapshot(
            rollups=RollupHealth(
                available=True,
                rows=0,
                max_rollup_date=None,
                observation_rows=32_231,
                max_observation_at=NOW,
                lag_days=None,
                unrolled_observations=32_231,
            )
        )
        alerts = by_id(evaluate(snap), "rollup.never_run")
        assert len(alerts) == 1
        assert alerts[0].severity is Severity.CRITICAL
        assert alerts[0].observed["observation_rows"] == 32_231

    def test_2b_rollup_lag_is_high_but_an_empty_system_is_not_alarmed(self) -> None:
        assert by_id(
            evaluate(healthy_snapshot(rollups=RollupHealth(available=True, rows=10, lag_days=5))),
            "rollup.stale",
        )[0].severity is Severity.HIGH
        # No observations at all => nothing to roll up => no alert.
        assert not by_id(
            evaluate(
                healthy_snapshot(
                    rollups=RollupHealth(available=True, rows=0, observation_rows=0)
                )
            ),
            "rollup.never_run",
        )

    def test_3_extra_com_discovery_runaway_is_critical(self) -> None:
        """Measured: extra.com ran 7,186 discovery runs on 2026-08-12 --
        after the runaway was believed fixed -- and burned ~$13/month."""
        snap = healthy_snapshot(
            discovery=(
                DomainDiscovery(domain="extra.com", runs_24h=7_186, runs_7d=9_157),
            )
        )
        alerts = evaluate(snap)
        volume = by_id(alerts, "discovery.runs_per_domain_per_day")
        assert volume and volume[0].severity is Severity.CRITICAL
        assert volume[0].subject == "extra.com"
        # ...and independently on rate-of-change.
        surge = by_id(alerts, "discovery.surge")
        assert surge and surge[0].severity is Severity.HIGH

    def test_3b_fqtoners_steady_state_runaway_is_caught_by_absolute_volume(self) -> None:
        """fqtoners.com did ~1,430 runs/day for ten consecutive days. Its
        rate-of-change is flat -- a surge rule alone would MISS it, which
        is exactly why absolute and rate rules both exist."""
        snap = healthy_snapshot(
            discovery=(
                DomainDiscovery(domain="fqtoners.com", runs_24h=1_430, runs_7d=10_010),
            )
        )
        alerts = evaluate(snap)
        assert by_id(alerts, "discovery.runs_per_domain_per_day")[
            0
        ].severity is Severity.CRITICAL
        # Flat rate: the surge rule correctly stays quiet.
        assert not by_id(alerts, "discovery.surge")

    def test_3c_surge_fires_below_any_absolute_ceiling(self) -> None:
        """The point of rate-of-change: 40 runs/day is under the 50/day
        ceiling, but it is a 5x jump from a standstill."""
        snap = healthy_snapshot(
            discovery=(DomainDiscovery(domain="jarir.com", runs_24h=40, runs_7d=88),)
        )
        alerts = evaluate(snap)
        assert not by_id(alerts, "discovery.runs_per_domain_per_day")
        assert by_id(alerts, "discovery.surge")

    def test_4_superuser_connection_reports_rls_as_inert(self) -> None:
        """Production connects as `postgres`; every FORCE ROW LEVEL
        SECURITY policy in the schema is decorative."""
        snap = healthy_snapshot(
            db_role=DatabaseRoleHealth(
                available=True,
                role="postgres",
                is_superuser=True,
                bypasses_rls=False,
            )
        )
        alerts = by_id(evaluate(snap), "security.rls_inert")
        assert len(alerts) == 1
        assert alerts[0].severity is Severity.CRITICAL
        assert alerts[0].category is Category.SECURITY
        assert alerts[0].observed["role"] == "postgres"

    def test_4b_bypassrls_role_also_reports_inert(self) -> None:
        snap = healthy_snapshot(
            db_role=DatabaseRoleHealth(
                available=True, role="app", is_superuser=False, bypasses_rls=True
            )
        )
        assert by_id(evaluate(snap), "security.rls_inert")

    def test_5_a_silent_pipeline_is_critical(self) -> None:
        """The fifth silent failure, found while validating against
        production: 2.6 days with no scrape attempt and zero refresh
        rules. Every threshold rule passes trivially on an empty window,
        so liveness needs its own rule."""
        snap = healthy_snapshot(
            freshness=Freshness(
                available=True,
                last_attempt_at=NOW - timedelta(hours=53),
                seconds_since_attempt=53 * 3600.0,
                refresh_rules_total=0,
                refresh_rules_enabled=0,
            )
        )
        alerts = evaluate(snap)
        assert by_id(alerts, "freshness.pipeline_silent")[0].severity is Severity.CRITICAL
        assert by_id(alerts, "freshness.no_refresh_rules")[0].severity is Severity.HIGH


# --------------------------------------------------------------------------
# Cost rules — the audit's four named primary alarms
# --------------------------------------------------------------------------


class TestCostAlarms:
    def test_requests_per_url_high_matches_the_breaker_threshold(self) -> None:
        """The warning must not fire only after the stop-loss has acted."""
        assert (
            DEFAULT_THRESHOLDS.requests_per_url_high == 8.0
        ), "keep in step with PROXY_BREAKER_MAX_REQUESTS_PER_URL"
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="stech.ink",
                    attempts=10_000,
                    successes=5_686,
                    distinct_urls=786,
                    proxied=5_714,
                    failed_paid=254,
                ),
            )
        )
        alerts = evaluate(snap)
        rpu = by_id(alerts, "cost.requests_per_url")
        assert rpu and rpu[0].severity is Severity.HIGH
        assert rpu[0].observed["requests_per_url"] == pytest.approx(12.72, abs=0.01)
        assert by_id(alerts, "cost.proxied_per_url")

    def test_requests_per_url_ignores_small_samples(self) -> None:
        """A ratio built on 10 attempts is not a measurement."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="tiny.example",
                    attempts=10,
                    successes=10,
                    distinct_urls=1,
                    proxied=0,
                    failed_paid=0,
                ),
            )
        )
        assert not by_id(evaluate(snap), "cost.requests_per_url")

    def test_healthy_amazon_ratio_does_not_fire(self) -> None:
        """2.48 req/URL is the measured healthy figure; it must be quiet."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="amazon.sa",
                    attempts=2_480,
                    successes=2_300,
                    distinct_urls=1_000,
                    proxied=2_480,
                    failed_paid=180,
                ),
            )
        )
        assert not by_id(evaluate(snap), "cost.requests_per_url")

    def test_spend_per_domain_per_day(self) -> None:
        """One domain outspending a whole catalog refresh in a day."""
        proxied = int(3.5 / cost.USD_PER_PROXIED_REQUEST)
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="amazon.sa",
                    attempts=proxied,
                    successes=proxied,
                    distinct_urls=proxied,  # keeps requests/url at 1.0
                    proxied=proxied,
                    failed_paid=0,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "cost.spend_per_domain_per_day")
        assert alerts and alerts[0].severity is Severity.HIGH
        assert alerts[0].observed["estimated_usd_24h"] == pytest.approx(3.5, abs=0.01)

    def test_wasted_paid_spend(self) -> None:
        """Measured amazon: 4,156 of 9,555 paid attempts produced nothing."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="amazon.sa",
                    attempts=10_449,
                    successes=5_399,
                    distinct_urls=10_449,
                    proxied=9_555,
                    failed_paid=4_156,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "cost.wasted_paid_rate")
        assert alerts and alerts[0].severity is Severity.HIGH
        assert alerts[0].observed["wasted_paid_rate"] == pytest.approx(0.435, abs=0.005)

    def test_month_end_forecast_from_velocity(self) -> None:
        """Absolute MTD spend is 8% of the ceiling and looks fine; the
        24h rate forecasts a breach. That gap is the extra.com lesson."""
        snap = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                proxied_month_to_date=20_900,
                proxied_24h=20_000,
                proxied_prev_24h=20_000,
                proxied_1h=0,
                proxied_prev_1h=0,
                seconds_remaining_in_month=16 * 86_400.0,
                ceiling_proxied_requests=250_000,
            )
        )
        alerts = by_id(evaluate(snap), "cost.month_end_forecast")
        assert alerts and alerts[0].severity is Severity.CRITICAL
        assert alerts[0].observed["month_to_date"] == 20_900
        assert alerts[0].observed["forecast_proxied"] > 250_000

    def test_forecast_uses_whichever_ceiling_binds_first(self) -> None:
        """Production had a breaker ceiling of 250,000 and an enforced
        provider ledger cap of 60,000. Forecasting against the larger
        number reports 'fine' while the request-level gate is closing."""
        snap = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                proxied_month_to_date=20_900,
                proxied_24h=3_000,
                proxied_prev_24h=3_000,
                seconds_remaining_in_month=16 * 86_400.0,
                ceiling_proxied_requests=250_000,
                provider_budget_limit=60_000,
            )
        )
        assert snap.spend.effective_ceiling == 60_000
        alerts = by_id(evaluate(snap), "cost.month_end_forecast")
        # 20,900 + 3,000*16 = 68,900 > 60,000 but far under 250,000.
        assert alerts and alerts[0].severity is Severity.CRITICAL
        assert alerts[0].observed["ceiling"] == 60_000

    def test_an_uncapped_active_provider_is_reported(self) -> None:
        """`limit is None` short-circuits before Redis: that provider's
        spend is never metered and never denied."""
        snap = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                seconds_remaining_in_month=1.0,
                ceiling_proxied_requests=250_000,
                providers_without_budget=1,
            )
        )
        alerts = by_id(evaluate(snap), "cost.provider_without_budget")
        assert alerts and alerts[0].severity is Severity.HIGH

    def test_spend_acceleration_needs_an_absolute_floor(self) -> None:
        """A 10x jump on trivial volume must not page anyone."""
        quiet = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                proxied_24h=100,
                proxied_prev_24h=10,
                seconds_remaining_in_month=1.0,
                ceiling_proxied_requests=250_000,
            )
        )
        assert not by_id(evaluate(quiet), "cost.spend_acceleration")

        loud = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                proxied_24h=20_000,
                proxied_prev_24h=2_000,
                seconds_remaining_in_month=1.0,
                ceiling_proxied_requests=250_000,
            )
        )
        alerts = by_id(evaluate(loud), "cost.spend_acceleration")
        assert alerts and alerts[0].observed["acceleration_24h"] == 10.0

    def test_acceleration_from_a_standstill_serialises_as_a_string(self) -> None:
        """`float('inf')` renders as the bare token `Infinity`, which is
        not valid JSON and which strict alert pipelines reject."""
        snap = healthy_snapshot(
            spend=SpendVelocity(
                available=True,
                proxied_24h=50_000,
                proxied_prev_24h=0,
                seconds_remaining_in_month=1.0,
                ceiling_proxied_requests=250_000,
            )
        )
        alerts = by_id(evaluate(snap), "cost.spend_acceleration")
        assert alerts and alerts[0].observed["acceleration_24h"] == "inf"


# --------------------------------------------------------------------------
# Cost per successful price (Task 2.4) — the audit's "denominator" alarm
# --------------------------------------------------------------------------


class TestCostPerSuccessfulPrice:
    """task-2.4 brief: 'cost per attempt is low, cost per usable price can
    be unbounded'. Two rules: a $/price ratio ceiling, and an unconditional
    alert on any spend that produced zero prices at all (the ratio is
    undefined/infinite there, so a ratio-only rule would never catch it)."""

    def test_threshold_matches_the_brief(self) -> None:
        assert DEFAULT_THRESHOLDS.cost_per_successful_price_high == 0.005

    def test_domain_with_spend_and_zero_prices_alerts(self) -> None:
        """The audit's named failure mode: money spent, nothing produced."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="amazon.sa",
                    attempts=25,
                    successes=0,
                    distinct_urls=15,
                    proxied=25,
                    failed_paid=25,
                    successful_prices=0,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "cost.zero_successful_prices")
        assert alerts
        assert alerts[0].severity is Severity.CRITICAL
        assert alerts[0].subject == "amazon.sa"
        assert alerts[0].observed["proxied_requests"] == 25
        assert alerts[0].observed["successful_prices"] == 0

    def test_healthy_domain_with_prices_and_spend_does_not_alert(self) -> None:
        """Plenty of spend, but priced efficiently -- must stay quiet."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="noon.com",
                    attempts=1_000,
                    successes=950,
                    distinct_urls=900,
                    proxied=1_000,
                    failed_paid=50,
                    successful_prices=950,
                ),
            )
        )
        alerts = evaluate(snap)
        assert not by_id(alerts, "cost.zero_successful_prices")
        assert not by_id(alerts, "cost.per_successful_price")

    def test_ratio_above_threshold_alerts_even_with_nonzero_prices(self) -> None:
        """Prices ARE produced, but the $/price ratio still breaches the
        ceiling -- a distinct condition from the zero-price case."""
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="amazon.sa",
                    attempts=10_000,
                    successes=10,
                    distinct_urls=10_000,
                    proxied=10_000,
                    failed_paid=9_990,
                    successful_prices=10,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "cost.per_successful_price")
        assert alerts
        assert alerts[0].severity is Severity.HIGH
        assert alerts[0].observed["cost_per_successful_price"] > 0.005
        assert alerts[0].observed["successful_prices"] == 10

    def test_a_domain_with_no_spend_at_all_is_quiet_regardless_of_prices(self) -> None:
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain="afaqalhasoob.com",
                    attempts=9,
                    successes=9,
                    distinct_urls=9,
                    proxied=0,
                    failed_paid=0,
                    successful_prices=0,
                ),
            )
        )
        alerts = evaluate(snap)
        assert not by_id(alerts, "cost.zero_successful_prices")
        assert not by_id(alerts, "cost.per_successful_price")


# --------------------------------------------------------------------------
# Controls that are inert are as bad as controls that are failing
# --------------------------------------------------------------------------


class TestInertControls:
    def test_missing_breaker_table_reports_the_stop_loss_as_not_in_effect(self) -> None:
        """Production is behind the code: `proxy_circuit_breakers` does not
        exist there, so the independent spend stop-loss is not deployed."""
        snap = healthy_snapshot(
            breaker=BreakerHealth(
                available=False,
                unavailable_reason='UndefinedTable: relation "proxy_circuit_breakers" '
                "does not exist",
            )
        )
        alerts = by_id(evaluate(snap), "breaker.unavailable")
        assert alerts and alerts[0].severity is Severity.HIGH
        assert alerts[0].category is Category.COST

    def test_stale_breaker_evaluator(self) -> None:
        snap = healthy_snapshot(
            breaker=BreakerHealth(
                available=True,
                scope_key="global",
                state="CLOSED",
                seconds_since_evaluation=7_200.0,
            )
        )
        assert by_id(evaluate(snap), "breaker.evaluator_stale")

    def test_open_breaker_is_critical_and_points_at_the_reset_procedure(self) -> None:
        snap = healthy_snapshot(
            breaker=BreakerHealth(
                available=True,
                scope_key="global",
                state="OPEN",
                trip_reason="DISCOVERY_RUNS_PER_DOMAIN",
                detail="extra.com ran 7186 discovery runs",
                trip_count=1,
                seconds_since_evaluation=60.0,
            )
        )
        alerts = by_id(evaluate(snap), "breaker.open")
        assert alerts and alerts[0].severity is Severity.CRITICAL
        assert alerts[0].runbook == "#close-the-breaker"

    def test_missing_outbox_table_is_reported(self) -> None:
        snap = healthy_snapshot(
            outbox=OutboxHealth(available=False, unavailable_reason="UndefinedTable")
        )
        assert by_id(evaluate(snap), "outbox.unavailable")

    def test_any_dead_outbox_message_alerts(self) -> None:
        snap = healthy_snapshot(outbox=OutboxHealth(available=True, pending=0, dead=1))
        assert by_id(evaluate(snap), "outbox.dead")[0].severity is Severity.HIGH

    def test_outbox_backlog_escalates(self) -> None:
        warn = evaluate(healthy_snapshot(outbox=OutboxHealth(available=True, pending=600)))
        assert by_id(warn, "outbox.backlog")[0].severity is Severity.WARNING
        high = evaluate(healthy_snapshot(outbox=OutboxHealth(available=True, pending=9_000)))
        assert by_id(high, "outbox.backlog")[0].severity is Severity.HIGH

    def test_stalled_outbox_dispatcher(self) -> None:
        snap = healthy_snapshot(
            outbox=OutboxHealth(
                available=True, pending=2, oldest_pending_age_seconds=3_600.0
            )
        )
        assert by_id(evaluate(snap), "outbox.oldest_pending_age")

    def test_redis_eviction_threshold_is_zero(self) -> None:
        """One evicted key can drop a match lock AND a spend counter."""
        snap = healthy_snapshot(
            redis=RedisHealth(
                available=True, policy_status="COMPLIANT", evicted_keys=1
            )
        )
        assert by_id(evaluate(snap), "redis.evictions")[0].severity is Severity.CRITICAL

    def test_redis_policy_violation_vs_unknown(self) -> None:
        violation = evaluate(
            healthy_snapshot(
                redis=RedisHealth(
                    available=True, policy_status="VIOLATION", policy="allkeys-lru"
                )
            )
        )
        assert by_id(violation, "redis.policy_violation")[0].severity is Severity.CRITICAL
        unknown = evaluate(
            healthy_snapshot(
                redis=RedisHealth(
                    available=True, policy_status="UNKNOWN", policy_detail="ACL"
                )
            )
        )
        assert by_id(unknown, "redis.policy_unknown")[0].severity is Severity.WARNING

    def test_redis_memory_pressure(self) -> None:
        snap = healthy_snapshot(
            redis=RedisHealth(
                available=True,
                policy_status="COMPLIANT",
                used_memory=900,
                maxmemory_from_info=1_000,
            )
        )
        assert by_id(evaluate(snap), "redis.memory")


# --------------------------------------------------------------------------
# Reliability
# --------------------------------------------------------------------------


class TestReliability:
    @pytest.mark.parametrize(
        ("domain", "attempts", "successes", "expected"),
        [
            # Measured 30d production figures.
            ("amazon.sa", 10_449, 5_402, Severity.HIGH),  # 51.7%
            ("noon.com", 6_100, 3_007, Severity.HIGH),  # 49.3%
            ("jarir.com", 1_707, 1_328, Severity.WARNING),  # 77.8%
            ("pcpalace.com.sa", 1_568, 1_541, None),  # 98.3%
        ],
    )
    def test_measured_domain_success_rates(
        self, domain: str, attempts: int, successes: int, expected: Severity | None
    ) -> None:
        snap = healthy_snapshot(
            domains_24h=(
                DomainStats(
                    domain=domain,
                    attempts=attempts,
                    successes=successes,
                    distinct_urls=attempts,
                    proxied=0,
                    failed_paid=0,
                ),
            )
        )
        alerts = by_id(evaluate(snap), "reliability.domain_success_rate")
        if expected is None:
            assert not alerts
        else:
            assert alerts and alerts[0].severity is expected

    def test_stuck_targets_by_status(self) -> None:
        snap = healthy_snapshot(
            queue=QueueHealth(
                available=True,
                targets_by_status={"PENDING": 5, "STARTED": 2, "DEFERRED": 16},
                oldest_pending_target_age_seconds=7_200.0,
                oldest_started_target_age_seconds=9_000.0,
                oldest_deferred_target_age_seconds=3_032_369.0,
            )
        )
        fired = ids(evaluate(snap))
        assert {
            "queue.pending_target_age",
            "queue.started_target_age",
            "queue.deferred_target_age",
        } <= fired

    def test_optimizer_churn(self) -> None:
        snap = healthy_snapshot(
            optimizer=OptimizerChurn(available=True, profiles_total=17, changed_24h=9)
        )
        assert by_id(evaluate(snap), "optimizer.churn")[0].severity is Severity.WARNING

    def test_scrapyd_saturation(self) -> None:
        snap = healthy_snapshot(
            scrapyd=ScrapydHealth(available=True, in_flight_targets=5_000)
        )
        assert by_id(evaluate(snap), "scrapyd.saturation")


# --------------------------------------------------------------------------
# Engine behaviour
# --------------------------------------------------------------------------


class TestEngine:
    def test_thresholds_are_overridable(self) -> None:
        snap = healthy_snapshot(
            discovery=(DomainDiscovery(domain="x.example", runs_24h=60, runs_7d=420),)
        )
        assert by_id(evaluate(snap), "discovery.runs_per_domain_per_day")
        relaxed = Thresholds(discovery_runs_per_day_high=1_000)
        assert not by_id(evaluate(snap, relaxed), "discovery.runs_per_domain_per_day")

    def test_a_broken_rule_does_not_silence_the_others(self) -> None:
        """The failure mode this whole module exists to prevent."""

        class Exploding:
            """Duck-types OpsSnapshot but raises on the domains section."""

            def __init__(self, inner: OpsSnapshot) -> None:
                self._inner = inner

            def __getattr__(self, name: str):
                if name == "domains_24h":
                    raise RuntimeError("boom")
                return getattr(self._inner, name)

        alerts = evaluate(
            Exploding(
                healthy_snapshot(
                    rollups=RollupHealth(
                        available=True, rows=0, observation_rows=100
                    )
                )
            )  # type: ignore[arg-type]
        )
        assert "rule.error" in ids(alerts)
        # ...and the unrelated CRITICAL still came through.
        assert "rollup.never_run" in ids(alerts)

    def test_alerts_are_ordered_most_severe_first(self) -> None:
        snap = healthy_snapshot(
            rollups=RollupHealth(available=True, rows=0, observation_rows=10),
            outbox=OutboxHealth(available=True, pending=600),
        )
        alerts = evaluate(snap)
        assert alerts[0].severity is Severity.CRITICAL
        assert worst_severity(alerts) is Severity.CRITICAL

    def test_every_alert_carries_a_justification(self) -> None:
        """A threshold nobody can defend gets ignored or deleted."""
        snap = healthy_snapshot(
            rollups=RollupHealth(available=True, rows=0, observation_rows=10),
            db_role=DatabaseRoleHealth(
                available=True, role="postgres", is_superuser=True, bypasses_rls=True
            ),
            discovery=(DomainDiscovery(domain="extra.com", runs_24h=9_000, runs_7d=9_100),),
        )
        alerts = evaluate(snap)
        assert alerts
        for alert in alerts:
            assert alert.justification.strip(), alert.rule_id
            assert alert.as_dict()["severity"] in {"CRITICAL", "HIGH", "WARNING"}

    def test_the_audit_named_signals_all_have_a_rule(self) -> None:
        """Audit §H5/§7 name these explicitly; none may be quietly dropped."""
        covered = {r.rule_id for r in RULES}
        for required in (
            "cost.requests_per_url",
            "cost.spend_per_domain_per_day",
            "cost.cost_per_successful_price",
            "cost.month_end_forecast",
            "discovery.*",
            "queue.*",
            "reliability.domain_success_rate",
            "redis.*",
            "breaker.*",
            "partition.*",
            "rollup.*",
            "outbox.*",
            "scrapyd.saturation",
            "optimizer.churn",
        ):
            assert required in covered, required


def test_every_runbook_anchor_resolves_to_a_real_heading() -> None:
    """An alert whose runbook link 404s is worse than no link: on-call
    follows it, finds nothing, and stops trusting the field.

    Audit §12 requires the runbook to exist; this keeps the alerts and
    the document from drifting apart silently, which is the same class
    of failure the whole module is about.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    runbook = repo_root / "docs" / "ops" / "RUNBOOK_STOP_DISPATCH_AND_SPEND.md"
    assert runbook.exists(), runbook
    anchors = set(re.findall(r"\{#([a-z0-9-]+)\}", runbook.read_text()))

    # Build a snapshot that trips as many distinct rules as possible so
    # the referenced anchors are collected from real alerts.
    snap = healthy_snapshot(
        partitions=(
            PartitionHealth(
                table="request_attempts",
                exists=True,
                current_month_present=False,
                next_month_present=False,
                partition_count=0,
                days_until_next_month=2,
            ),
        ),
        rollups=RollupHealth(available=True, rows=0, observation_rows=10),
        outbox=OutboxHealth(available=True, pending=9_000, dead=3,
                            oldest_pending_age_seconds=99_999.0),
        breaker=BreakerHealth(
            available=True, scope_key="global", state="OPEN",
            trip_reason="MONTHLY_SPEND", detail="x",
            seconds_since_evaluation=99_999.0,
        ),
        queue=QueueHealth(
            available=True,
            targets_by_status={"PENDING": 1, "STARTED": 1, "DEFERRED": 1},
            oldest_pending_target_age_seconds=99_999.0,
            oldest_started_target_age_seconds=99_999.0,
            oldest_deferred_target_age_seconds=99_999.0,
            oldest_pending_job_age_seconds=99_999.0,
            jobs_pending=1,
        ),
        domains_24h=(
            DomainStats(domain="amazon.sa", attempts=50_000, successes=100,
                        distinct_urls=1_000, proxied=50_000, failed_paid=49_000),
        ),
        discovery=(DomainDiscovery(domain="extra.com", runs_24h=9_000, runs_7d=9_100),),
        spend=SpendVelocity(
            available=True, proxied_month_to_date=999_999,
            proxied_24h=99_999, proxied_prev_24h=10,
            seconds_remaining_in_month=86_400.0,
            ceiling_proxied_requests=250_000, providers_without_budget=1,
        ),
        freshness=Freshness(available=True, seconds_since_attempt=999_999.0,
                            refresh_rules_total=0, refresh_rules_enabled=0),
        optimizer=OptimizerChurn(available=True, profiles_total=17, changed_24h=17),
        redis=RedisHealth(available=True, policy_status="VIOLATION",
                          policy="allkeys-lru", evicted_keys=5,
                          used_memory=99, maxmemory_from_info=100),
        scrapyd=ScrapydHealth(available=True, in_flight_targets=99_999),
        db_role=DatabaseRoleHealth(available=True, role="postgres",
                                   is_superuser=True, bypasses_rls=True),
    )
    referenced = {a.runbook.lstrip("#") for a in evaluate(snap) if a.runbook}
    assert referenced, "expected this snapshot to fire runbook-linked alerts"
    assert referenced <= anchors, referenced - anchors
