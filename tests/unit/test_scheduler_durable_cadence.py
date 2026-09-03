"""Scheduler wiring for the durable daily cadences (2026-08-15 readiness cycle).

The defect: `main()` drove `partition_create` / `daily_rollup` /
`retention_drop` off in-process float accumulators re-initialised to
``0.0`` on every process start, against an 86400s interval. On Railway a
deploy is a restart, so those three tasks could go — and did go —
arbitrarily long without firing. `test_main_no_longer_uses_in_process_
accumulators_for_daily_cadences` fails against that code by construction.

Loaded in a fresh subprocess (mirrors `test_scheduler_outbox_wiring.py`)
because `apps/api`, `apps/workers` and `apps/scheduler` each ship their
own top-level `app` package.
"""

from __future__ import annotations

import os
import subprocess
import sys

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

_SETUP = """
import sys
sys.path.insert(0, "apps/scheduler")

from contextlib import contextmanager

from app.scheduler import scheduler_app
from app_shared.config import get_settings
from app_shared.task_names import (
    MAINTENANCE_BREAKER_EVALUATE,
    MAINTENANCE_COST_ROLLUP,
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_ENTITLEMENT_REFRESH,
    MAINTENANCE_FLEET_BUDGET_ROLLFORWARD,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RECONCILE_PROVIDER_USAGE,
    MAINTENANCE_RETENTION_DROP,
)
from app_shared.models.maintenance_cadence import (
    CADENCE_BREAKER_EVALUATE,
    CADENCE_COST_ROLLUP,
    CADENCE_DAILY_ROLLUP,
    CADENCE_ENTITLEMENT_REFRESH,
    CADENCE_FLEET_BUDGET_ROLLFORWARD,
    CADENCE_PARTITION_CREATE,
    CADENCE_RECONCILE_PROVIDER_USAGE,
    CADENCE_RETENTION_DROP,
)


class _RecordingEnqueue:
    def __init__(self):
        self.calls = []

    def __call__(self, name, *, queue, kwargs=None):
        self.calls.append((name, queue))


class _FakeSession:
    def __init__(self):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _install(claim_keys, session=None):
    \"\"\"Wire scheduler_app onto a fake system session + a claim function
    that only grants `claim_keys`.\"\"\"
    session = session or _FakeSession()
    scheduler_app.get_system_sessionmaker = lambda: (lambda: session)
    scheduler_app.ensure_cadence_rows = lambda s: None
    scheduler_app.claim_cadence = (
        lambda s, key, *, interval_seconds, now=None: key in claim_keys
    )
    enqueue = _RecordingEnqueue()
    scheduler_app.enqueue = enqueue
    return enqueue, session
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_main_no_longer_uses_in_process_accumulators_for_daily_cadences() -> None:
    """THE regression guard. A float that resets on process start must
    never again be the only thing standing between the calendar and a
    total write outage. Fails against the pre-fix `main()`."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
for banned in (
    "partition_create_elapsed",
    "daily_rollup_elapsed",
    "retention_elapsed",
):
    assert banned not in source, banned

for required in (
    "_run_durable_cadence_tick(settings)",
    "_run_health_tick(settings)",
    "MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS",
    "MAINTENANCE_HEALTH_INTERVAL_SECONDS",
):
    assert required in source, required
print("OK")
"""
        )
    )


def test_boot_runs_the_cadence_and_health_passes_before_the_loop() -> None:
    """An overdue cadence must fire promptly on boot, not one poll
    interval later — and a partition gap must be reported at once."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
boot = source.split("while not _shutdown_requested")[0]
assert "_run_durable_cadence_tick(settings)" in boot
assert "_run_health_tick(settings)" in boot
print("OK")
"""
        )
    )


def test_durable_cadences_cover_all_three_daily_tasks() -> None:
    """The original three (2026-08-15 readiness cycle)."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]

assert keys[:3] == [CADENCE_PARTITION_CREATE, CADENCE_DAILY_ROLLUP, CADENCE_RETENTION_DROP], keys
assert attrs[:3] == [
    "PARTITION_CREATE_INTERVAL_SECONDS",
    "DAILY_ROLLUP_INTERVAL_SECONDS",
    "RETENTION_INTERVAL_SECONDS",
], attrs
print("OK")
"""
        )
    )


def test_durable_cadences_also_cover_the_epa_c5_c6_owed_wiring() -> None:
    """EPA C5 registered `MAINTENANCE_RECONCILE_PROVIDER_USAGE` and
    implemented the task but could not wire its schedule entry
    (`apps/scheduler` was fenced then). EPA C6 closes that gap alongside
    wiring its own `MAINTENANCE_COST_ROLLUP` cadence — additive entries,
    the original three untouched (see the test above)."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
fns = [c[2].__name__ for c in scheduler_app._DURABLE_CADENCES]

assert CADENCE_RECONCILE_PROVIDER_USAGE in keys, keys
assert CADENCE_COST_ROLLUP in keys, keys
assert keys[3:5] == [CADENCE_RECONCILE_PROVIDER_USAGE, CADENCE_COST_ROLLUP], keys
assert fns[3:5] == ["_enqueue_reconcile_provider_usage", "_enqueue_cost_rollup"], fns
print("OK")
"""
        )
    )


def test_reconcile_provider_usage_and_cost_rollup_cadences_are_claimable_and_enqueue() -> None:
    """Behavioral proof, not just a static list check: when claimed, each
    new cadence enqueues its OWN task name."""
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_RECONCILE_PROVIDER_USAGE, CADENCE_COST_ROLLUP})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert set(claimed) == {CADENCE_RECONCILE_PROVIDER_USAGE, CADENCE_COST_ROLLUP}, claimed
assert set(enqueue.calls) == {
    (MAINTENANCE_RECONCILE_PROVIDER_USAGE, "maintenance"),
    (MAINTENANCE_COST_ROLLUP, "maintenance"),
}, enqueue.calls
print("OK")
"""
        )
    )


def test_tick_enqueues_only_the_cadences_it_actually_claimed() -> None:
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_PARTITION_CREATE, CADENCE_RETENTION_DROP})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [CADENCE_PARTITION_CREATE, CADENCE_RETENTION_DROP], claimed
assert enqueue.calls == [
    (MAINTENANCE_PARTITION_CREATE, "maintenance"),
    (MAINTENANCE_RETENTION_DROP, "maintenance"),
], enqueue.calls
print("OK")
"""
        )
    )


def test_tick_enqueues_nothing_when_no_cadence_is_due() -> None:
    """The steady state: a scheduler polling every 60s against three
    daily deadlines must sit silent, not spam the maintenance queue."""
    _assert_ok(
        _run(
            """
enqueue, session = _install(set())

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [], claimed
assert enqueue.calls == [], enqueue.calls
print("OK")
"""
        )
    )


def test_a_database_failure_in_the_tick_never_crashes_the_scheduler() -> None:
    """A failed tick is retried on the next poll — and, unlike the
    accumulator design, it no longer LOSES the cadence: the deadline is
    still sitting unclaimed in Postgres."""
    _assert_ok(
        _run(
            """
def _boom():
    raise RuntimeError("SYSTEM_DATABASE_URL is required")

scheduler_app.get_system_sessionmaker = _boom

claimed = scheduler_app._run_durable_cadence_tick(get_settings())  # must not raise
assert claimed == [], claimed

scheduler_app._run_health_tick(get_settings())  # must not raise either
print("OK")
"""
        )
    )


def test_partition_lookahead_gives_months_of_margin_not_days() -> None:
    """With a lookahead of 1 the entire margin between "maintenance
    stopped" and "every INSERT fails" is whatever is left of the current
    month. The whole point of the fix is that the margin outlives a slow
    detection cycle."""
    _assert_ok(
        _run(
            """
from app_shared.config import Settings

lookahead = Settings.model_fields["PARTITION_CREATE_LOOKAHEAD_MONTHS"].default
assert lookahead >= 3, lookahead

poll = Settings.model_fields["MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS"].default
daily = Settings.model_fields["PARTITION_CREATE_INTERVAL_SECONDS"].default
assert poll < daily, (poll, daily)
print("OK")
"""
        )
    )


def test_durable_cadences_also_cover_the_seeded_entitlement_refresh() -> None:
    """EPA go-live prep (2026-08-26). The C3 gate treats stale evidence as
    inactive, so the placeholder rows `scripts/seed_workspace_entitlements
    .py` writes must be re-stamped on a cadence or the whole fleet is
    denied 24h after the deploy that seeded it. Additive entry — the
    original three and the C5/C6 pair are untouched (tests above)."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]
fns = [c[2].__name__ for c in scheduler_app._DURABLE_CADENCES]

assert CADENCE_ENTITLEMENT_REFRESH in keys, keys
index = keys.index(CADENCE_ENTITLEMENT_REFRESH)
assert fns[index] == "_enqueue_entitlement_refresh", fns
# Its OWN interval knob, not the daily one it must stay far under.
assert attrs[index] == "ENTITLEMENT_REFRESH_INTERVAL_SECONDS", attrs
print("OK")
"""
        )
    )


def test_entitlement_refresh_cadence_is_claimable_and_enqueues_its_own_task() -> None:
    """Behavioral proof, not a list check: a claimed cadence must enqueue
    `MAINTENANCE_ENTITLEMENT_REFRESH` on the `maintenance` queue."""
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_ENTITLEMENT_REFRESH})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [CADENCE_ENTITLEMENT_REFRESH], claimed
assert enqueue.calls == [(MAINTENANCE_ENTITLEMENT_REFRESH, "maintenance")], enqueue.calls
print("OK")
"""
        )
    )


def test_entitlement_refresh_interval_is_far_under_the_staleness_deadline() -> None:
    """The arithmetic that makes this cadence worth having. If the refresh
    interval ever reached `DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS`,
    a single missed tick would deny every workspace all paid work."""
    _assert_ok(
        _run(
            """
from app_shared.costauth.service import DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS

interval = get_settings().ENTITLEMENT_REFRESH_INTERVAL_SECONDS
assert interval > 0, interval
assert interval * 4 <= DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS, interval
print("OK")
"""
        )
    )


def test_durable_cadences_cover_the_breaker_evaluator() -> None:
    """EPA B1 (2026-09-03). The proxy breaker's `evaluated_at` is evidence
    the cost gate fails CLOSED on once it passes
    `DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS` — and until this entry the
    ONLY thing that refreshed it ran inside the scraping path that the
    very same gate blocks. An idle (or already-denied) fleet therefore let
    the evidence rot and then denied all paid work permanently, with no
    process anywhere able to break the loop. A scheduler-driven cadence is
    the break: it evaluates whether or not anything is scraping."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]
fns = [c[2].__name__ for c in scheduler_app._DURABLE_CADENCES]

assert CADENCE_BREAKER_EVALUATE in keys, keys
index = keys.index(CADENCE_BREAKER_EVALUATE)
assert fns[index] == "_enqueue_breaker_evaluate", fns
# Its own knob — the same one the in-scrape evaluator's lease already
# uses, so the cadence and the lease can never disagree about how often
# an evaluation is due.
assert attrs[index] == "PROXY_BREAKER_EVAL_INTERVAL_SECONDS", attrs
print("OK")
"""
        )
    )


def test_breaker_evaluate_interval_is_far_under_the_staleness_deadline() -> None:
    """The arithmetic that makes this cadence worth having. Evidence must be
    refreshed several times inside the staleness window, so losing a run (or
    three) still cannot age the row past
    `DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS` and deny every paid path."""
    _assert_ok(
        _run(
            """
from app_shared.costauth.service import DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS

keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]
attr = attrs[keys.index(CADENCE_BREAKER_EVALUATE)]

interval = getattr(get_settings(), attr)
assert interval > 0, interval
assert interval * 4 <= DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS, (attr, interval)
print("OK")
"""
        )
    )


def test_breaker_evaluate_cadence_enqueues_its_own_task() -> None:
    """Behavioral proof, not a list check: a claimed cadence must enqueue
    `MAINTENANCE_BREAKER_EVALUATE` on the `maintenance` queue."""
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_BREAKER_EVALUATE})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [CADENCE_BREAKER_EVALUATE], claimed
assert enqueue.calls == [(MAINTENANCE_BREAKER_EVALUATE, "maintenance")], enqueue.calls
print("OK")
"""
        )
    )


def test_durable_cadences_cover_the_fleet_budget_rollforward() -> None:
    """EPA A4/B3 (2026-09-03). `fleet_cost_budgets` is the only fleet-wide
    money ceiling there is, and it was seeded for a FIXED set of months
    ending `2026_10`. A budget row is born with `NULL` limits, so the
    first paid dispatch of the month after the last one seeded creates an
    uncapped row and the ceiling is gone — with no denial, no log line and
    the provider bill as the only symptom. This cadence is what makes the
    ceiling survive a month boundary without an operator."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]
fns = [c[2].__name__ for c in scheduler_app._DURABLE_CADENCES]

assert CADENCE_FLEET_BUDGET_ROLLFORWARD in keys, keys
index = keys.index(CADENCE_FLEET_BUDGET_ROLLFORWARD)
assert fns[index] == "_enqueue_fleet_budget_rollforward", fns
# Deliberately shares the 6h entitlement knob rather than adding one:
# the deadline this cadence races is a MONTH boundary, so six hours is
# three orders of magnitude of margin and a knob of its own would be a
# setting with no decision behind it.
assert attrs[index] == "ENTITLEMENT_REFRESH_INTERVAL_SECONDS", attrs
print("OK")
"""
        )
    )


def test_fleet_budget_rollforward_interval_is_far_under_a_month() -> None:
    """The arithmetic that makes the cadence worth having: many ticks
    inside every month, so losing several in a row still cannot let a
    month boundary be crossed with an uncapped budget row."""
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]
attr = attrs[keys.index(CADENCE_FLEET_BUDGET_ROLLFORWARD)]

interval = getattr(get_settings(), attr)
assert interval > 0, interval
# 28 days is the shortest month.
assert interval * 20 <= 28 * 86400, (attr, interval)
print("OK")
"""
        )
    )


def test_fleet_budget_rollforward_cadence_enqueues_its_own_task() -> None:
    """Behavioral proof, not a list check: a claimed cadence must enqueue
    `MAINTENANCE_FLEET_BUDGET_ROLLFORWARD` on the `maintenance` queue."""
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_FLEET_BUDGET_ROLLFORWARD})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [CADENCE_FLEET_BUDGET_ROLLFORWARD], claimed
assert enqueue.calls == [
    (MAINTENANCE_FLEET_BUDGET_ROLLFORWARD, "maintenance")
], enqueue.calls
print("OK")
"""
        )
    )
