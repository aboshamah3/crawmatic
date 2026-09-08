"""Per-task Celery time limits (EPA B4, F09).

Without a `time_limit`, a runaway task (a bug, a hung socket a
lower-level timeout failed to catch) can run past
`CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS` and get redelivered to a
second worker while the first is still executing it -- running the same
work twice. This asserts the invariant `apps/workers/app/workers/
celery_app.py`'s `task_annotations` block exists to guarantee: EVERY task
registered on `app.tasks` (the real ones -- Celery's own built-ins like
`celery.chord`/`celery.backend_cleanup` are excluded, this repo does not
own their time budget) carries a `time_limit` strictly below the
visibility timeout.

Loaded in a fresh subprocess (the `test_jobs_dispatch_task.py` /
`test_celery_delivery_reliability.py` precedent): `apps/api`/`apps/
workers` each ship a top-level `app` package, and `celery_app.py` calls
`get_settings()` at module scope.
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

# Every task module Celery's `include=[...]` list eagerly imports at
# worker startup -- imported explicitly here so `app.tasks` is fully
# populated the same way it would be inside a running worker (the
# `test_celery_delivery_reliability.py::
# test_outbox_tasks_are_registered_and_routed_to_maintenance` precedent).
# `app.workers.tasks_dispatch` is deliberately NOT imported directly: it
# registers transitively via `tasks_jobs`'s own import of it, exactly as
# it does inside a real worker process.
_SETUP = """
import sys
sys.path.insert(0, "apps/workers")

from app.workers.celery_app import app
celery_app = app  # `import app.workers...` below rebinds the name `app`
from app_shared.config import get_settings

import app.workers.tasks_jobs  # noqa: F401,E402
import app.workers.tasks_analysis  # noqa: F401,E402
import app.workers.tasks_strategy  # noqa: F401,E402
import app.workers.tasks_maintenance  # noqa: F401,E402
import app.workers.tasks_webhooks  # noqa: F401,E402
import app.workers.tasks_outbox  # noqa: F401,E402

app = celery_app
settings = get_settings()

# Celery's own internal task names (chord/group/chain/backend_cleanup
# helpers) are not this repo's tasks and carry no `time_limit` of their
# own -- excluded from the "every registered task" sweep below.
_OURS = [name for name in app.tasks if not name.startswith("celery.")]
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


def test_at_least_the_expected_task_count_is_registered() -> None:
    """Sanity floor so a future refactor that accidentally stops
    `include=`ing a module cannot make the sweep below pass vacuously."""
    _assert_ok(
        _run(
            """
assert len(_OURS) >= 24, sorted(_OURS)
print("OK")
"""
        )
    )


def test_every_registered_task_has_a_time_limit_below_the_visibility_timeout() -> None:
    _assert_ok(
        _run(
            """
visibility = settings.CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
assert visibility == 3600, visibility

missing = [name for name in _OURS if app.tasks[name].time_limit is None]
assert not missing, f"tasks with no time_limit at all: {sorted(missing)}"

too_high = {
    name: app.tasks[name].time_limit
    for name in _OURS
    if not (app.tasks[name].time_limit < visibility)
}
assert not too_high, f"tasks with time_limit >= visibility timeout: {too_high}"
print("OK")
"""
        )
    )


def test_soft_time_limit_is_set_and_below_the_hard_limit_for_every_task() -> None:
    _assert_ok(
        _run(
            """
bad = {
    name: (app.tasks[name].soft_time_limit, app.tasks[name].time_limit)
    for name in _OURS
    if not (
        app.tasks[name].soft_time_limit is not None
        and app.tasks[name].soft_time_limit < app.tasks[name].time_limit
    )
}
assert not bad, f"tasks missing a soft_time_limit below their hard limit: {bad}"
print("OK")
"""
        )
    )


def test_dispatch_family_time_limit_is_600_seconds() -> None:
    """Plan-named bucket: dispatch 600s."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import SCRAPE_DISPATCH_JOB
assert app.tasks[SCRAPE_DISPATCH_JOB].time_limit == 600
assert app.tasks["dispatch.generic_price_spider"].time_limit == 600
print("OK")
"""
        )
    )


def test_reaper_reconciler_family_time_limit_is_300_seconds() -> None:
    """Plan-named bucket: reapers/reconcilers 300s."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import (
    SCRAPE_RECOVER_STALLED,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_REDISPATCH_JOBS,
    SCRAPE_RECONCILE_FALSE_FAILURES,
    SCRAPE_REAP_STALE_TARGETS,
)
for name in (
    SCRAPE_RECOVER_STALLED,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_REDISPATCH_JOBS,
    SCRAPE_RECONCILE_FALSE_FAILURES,
    SCRAPE_REAP_STALE_TARGETS,
):
    assert app.tasks[name].time_limit == 300, (name, app.tasks[name].time_limit)
print("OK")
"""
        )
    )


def test_breaker_time_limit_is_120_seconds() -> None:
    """Plan-named bucket: breaker 120s."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import MAINTENANCE_BREAKER_EVALUATE
assert app.tasks[MAINTENANCE_BREAKER_EVALUATE].time_limit == 120
print("OK")
"""
        )
    )


def test_webhooks_time_limit_is_120_seconds() -> None:
    """Plan-named bucket: webhooks 120s."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import CREATE_WEBHOOK_EVENT
assert app.tasks[CREATE_WEBHOOK_EVENT].time_limit == 120
print("OK")
"""
        )
    )


def test_rollup_time_limit_is_1800_seconds_per_chunk() -> None:
    """Plan-named bucket: rollups 1,800s per chunk."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import MAINTENANCE_DAILY_ROLLUP
assert app.tasks[MAINTENANCE_DAILY_ROLLUP].time_limit == 1800
print("OK")
"""
        )
    )


def test_discovery_time_limit_is_900_seconds_per_chunk() -> None:
    """Plan-named bucket: discovery 900s per chunk."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import STRATEGY_DISCOVERY_RUN
assert app.tasks[STRATEGY_DISCOVERY_RUN].time_limit == 900
print("OK")
"""
        )
    )


def test_analysis_time_limit_is_900_seconds() -> None:
    """Plan-named bucket: analysis 900s."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE
assert app.tasks[PRICE_ANALYSIS_RECOMPUTE].time_limit == 900
print("OK")
"""
        )
    )


def test_discovery_scan_is_registered_with_a_time_limit() -> None:
    """The new B4 chunked-scan task itself must carry a limit too."""
    _assert_ok(
        _run(
            """
from app_shared.task_names import STRATEGY_DISCOVERY_SCAN
assert STRATEGY_DISCOVERY_SCAN in app.tasks
assert app.tasks[STRATEGY_DISCOVERY_SCAN].time_limit == 300
print("OK")
"""
        )
    )
