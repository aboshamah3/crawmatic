"""`MAINTENANCE_DOMAIN_TIMEOUT_TUNE` — per-domain timeouts from success latency.

The deep dive's "46.9 s average proxied-HTTP attempt" is not a latency
measurement; it measures `SCRAPE_DOWNLOAD_TIMEOUT_SECONDS` (60 s), the
single global ceiling every domain shares. Every fetch that was going to
fail therefore burns the full minute of a worker slot, a fleet lease and
real proxy egress before anyone learns anything, while a domain that
answers in ~1 s gains nothing from being allowed sixty.

These tests pin the policy — `clamp(1.5 x p95(successful attempt
duration, 7 d), 10 s, 60 s)` — and, just as importantly, the three
things it refuses to do: invent a value for a domain it cannot measure,
ever exceed the global default, and write a row it did not change.
"""

from __future__ import annotations

from app_shared.maintenance.domain_timeouts import (
    DOMAIN_TIMEOUT_MAX_SECONDS,
    DOMAIN_TIMEOUT_MIN_SAMPLE,
    DOMAIN_TIMEOUT_MIN_SECONDS,
    SUCCESS_LATENCY_P95_SQL,
    clamp_domain_timeout,
    plan_domain_timeouts,
)
from app_shared.task_names import MAINTENANCE_DOMAIN_TIMEOUT_TUNE


def _row(domain: str, sample_size: int, p95_ms: float | None):
    return (domain, sample_size, p95_ms)


# --- the clamp --------------------------------------------------------------


def test_headroom_is_one_and_a_half_times_p95() -> None:
    # 20 s p95 -> 30 s, comfortably inside both bounds.
    assert clamp_domain_timeout(20.0) == 30


def test_a_fast_domain_is_floored_not_starved() -> None:
    """1.5 x 1.2 s = 1.8 s; one bad minute must not read as an outage."""
    assert clamp_domain_timeout(1.2) == DOMAIN_TIMEOUT_MIN_SECONDS


def test_a_slow_domain_can_never_exceed_the_global_default() -> None:
    """The ceiling IS today's default: this task only ever tightens."""
    assert clamp_domain_timeout(120.0) == DOMAIN_TIMEOUT_MAX_SECONDS


def test_rounding_is_up_because_a_timeout_is_a_ceiling() -> None:
    # 1.5 x 20.2 = 30.3 -> 31, never 30: rounding down would cut off the
    # very requests the headroom was bought for.
    assert clamp_domain_timeout(20.2) == 31


# --- the plan ---------------------------------------------------------------


def test_a_measurable_domain_gets_its_own_timeout() -> None:
    plans = plan_domain_timeouts([_row("noon.com", 500, 4000.0)])
    assert len(plans) == 1
    plan = plans[0]
    assert plan.domain == "noon.com"
    assert plan.p95_seconds == 4.0
    assert plan.timeout_seconds == 10  # 1.5 x 4 s = 6 s -> floored to 10
    assert plan.sample_size == 500


def test_a_domain_with_too_few_successes_is_left_alone() -> None:
    """NULL already means "use the setting" -- an invented value is worse."""
    assert plan_domain_timeouts([_row("rare.example", DOMAIN_TIMEOUT_MIN_SAMPLE - 1, 8000.0)]) == []


def test_a_domain_with_no_measurable_p95_is_left_alone() -> None:
    assert plan_domain_timeouts([_row("quiet.example", 900, None)]) == []
    assert plan_domain_timeouts([_row("weird.example", 900, 0.0)]) == []


def test_an_unchanged_domain_is_reported_unchanged() -> None:
    """A steady-state run must write nothing."""
    (plan,) = plan_domain_timeouts(
        [_row("noon.com", 500, 24_000.0)], current={"noon.com": 36}
    )
    assert plan.timeout_seconds == 36
    assert plan.changed is False


def test_a_domain_with_no_row_yet_counts_as_changed() -> None:
    (plan,) = plan_domain_timeouts([_row("amazon.sa", 500, 24_000.0)], current={})
    assert plan.current_seconds is None
    assert plan.changed is True


def test_rows_can_be_orm_shaped_too() -> None:
    class _Row:
        domain = "noon.com"
        sample_size = 500
        p95_ms = 4000.0

    (plan,) = plan_domain_timeouts([_Row()])
    assert plan.domain == "noon.com"


# --- the SQL's own guarantees ----------------------------------------------


def test_p95_reads_only_successful_scrape_attempts() -> None:
    """Failures are dominated by the CURRENT timeout, so including them
    would make the new timeout a function of the old one and ratchet it
    upward forever. Discovery probes deliberately try transports that do
    not work, so their latency describes the probe, not the domain."""
    sql = " ".join(SUCCESS_LATENCY_P95_SQL.split())
    assert "ra.success IS TRUE" in sql
    assert "ra.origin = 'SCRAPE'" in sql
    assert "percentile_cont(0.95)" in sql


def test_domain_comes_from_the_competitor_not_the_url() -> None:
    """`domain_rules.domain` stores `competitors.domain` byte-for-byte;
    parsing the attempt's URL would file a redirect under the wrong
    domain and create a second, silently-shadowing row."""
    sql = " ".join(SUCCESS_LATENCY_P95_SQL.split())
    assert "JOIN competitors AS c ON c.id = m.competitor_id" in sql
    assert "c.domain AS domain" in sql


def test_the_window_is_a_bound_parameter_not_interpolated() -> None:
    sql = " ".join(SUCCESS_LATENCY_P95_SQL.split())
    assert "make_interval(days => :window_days)" in sql


# --- registration -----------------------------------------------------------


def test_task_name_follows_the_maintenance_convention() -> None:
    assert MAINTENANCE_DOMAIN_TIMEOUT_TUNE == "maintenance.domain_timeout_tune"


def test_the_task_is_routed_and_time_limited_like_its_siblings() -> None:
    """B4's invariant: every registered task carries a `time_limit`."""
    import ast
    import pathlib

    source = pathlib.Path("apps/workers/app/workers/celery_app.py").read_text()
    # Textual, not by import: `celery_app.py` calls `get_settings()` at
    # module scope, which needs a full env (see `test_celery_time_limits`).
    assert "MAINTENANCE_DOMAIN_TIMEOUT_TUNE: {\"queue\": \"maintenance\"}" in source
    assert "MAINTENANCE_DOMAIN_TIMEOUT_TUNE: _REAPER_LIMITS" in source
    ast.parse(source)


def test_the_column_it_writes_exists_on_the_model() -> None:
    from app_shared.models.domain_rules import DomainRule

    column = DomainRule.__table__.c.request_timeout_seconds
    assert column.nullable is True, "NULL means 'use the setting'"
    assert column.server_default is None, "additive: no rewrite, no backfill"
