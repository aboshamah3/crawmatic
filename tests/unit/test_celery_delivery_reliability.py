"""Celery delivery-reliability configuration (2026-08-15 audit risk H1).

Audit §H1: "Celery defines queues and routing but not late acknowledgement,
reject-on-worker-lost, broker visibility timeout, or a reliability-oriented
prefetch policy." These tests pin each of those four settings, plus the
routing of the two new outbox passes, so a future edit cannot quietly
revert the durability posture.

They also pin the *reasoning*, not just the values: the visibility
timeout must exceed the measured worst-case task runtime by a real
margin, and prefetch must be the fair (1) setting rather than the
batching default, because with late acks a prefetched message is held
unacknowledged and a dead worker strands everything it hoarded.

Loaded in a fresh subprocess (mirrors `test_jobs_dispatch_task.py`) for
the usual two reasons: `apps/api`/`apps/workers` each ship a top-level
`app` package, and `celery_app.py` calls `get_settings()` at module scope.
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
sys.path.insert(0, "apps/workers")

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.task_names import (
    OUTBOX_DRAIN,
    OUTBOX_RECONCILE,
    STRATEGY_DISCOVERY_RUN,
)

settings = get_settings()
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


def test_late_acknowledgement_is_enabled() -> None:
    """Without this, a worker killed mid-task loses the task outright:
    the message is acked on delivery, so nothing ever redelivers it."""
    _assert_ok(
        _run(
            """
assert app.conf.task_acks_late is True, app.conf.task_acks_late
print("OK")
"""
        )
    )


def test_worker_loss_rejection_is_enabled() -> None:
    """A prefork child killed by the OOM killer (or by
    `worker_max_memory_per_child`) must requeue its task, not fail it."""
    _assert_ok(
        _run(
            """
assert app.conf.task_reject_on_worker_lost is True, app.conf.task_reject_on_worker_lost
print("OK")
"""
        )
    )


def test_application_errors_are_still_acknowledged() -> None:
    """Redelivery is for lost workers, not for reproducible exceptions —
    otherwise a poison task loops forever."""
    _assert_ok(
        _run(
            """
assert app.conf.task_acks_on_failure_or_timeout is True
print("OK")
"""
        )
    )


def test_visibility_timeout_is_set_and_exceeds_the_longest_task() -> None:
    """The redelivery clock must be longer than the slowest task, or a
    healthy long task gets redelivered mid-flight and runs twice.

    Worst case measured in this codebase is `STRATEGY_DISCOVERY_RUN`'s
    probe loop: the access ladder is DIRECT_HTTP (1 request) +
    DIRECT_HTTP_RETRY (2) + PROXY_HTTP (1) = 4 requests per URL, over up
    to `STRATEGY_DISCOVERY_MAX_SAMPLE` URLs, each capped by the module's
    15s probe timeout.
    """
    _assert_ok(
        _run(
            """
options = app.conf.broker_transport_options or {}
visibility = options.get("visibility_timeout")
assert visibility is not None, "no visibility_timeout configured"
assert visibility == settings.CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS

probe_timeout_seconds = 15
requests_per_url = 4
worst_case = probe_timeout_seconds * requests_per_url * settings.STRATEGY_DISCOVERY_MAX_SAMPLE
assert worst_case == 600, worst_case
assert visibility > worst_case, (visibility, worst_case)
# Real margin, not a hairline: at least 3x the worst case.
assert visibility >= 3 * worst_case, (visibility, worst_case)
print("OK")
"""
        )
    )


def test_prefetch_multiplier_is_the_fair_setting() -> None:
    """1, not the default 4: with late acks a prefetched message is held
    unacknowledged, so hoarding multiplies what a dead worker strands for
    a full visibility timeout. Tasks here run seconds-to-minutes while a
    broker round-trip is ~1ms, so batching buys nothing."""
    _assert_ok(
        _run(
            """
assert app.conf.worker_prefetch_multiplier == 1, app.conf.worker_prefetch_multiplier
assert app.conf.worker_prefetch_multiplier == settings.CELERY_WORKER_PREFETCH_MULTIPLIER
# At concurrency 4 that bounds in-flight-per-worker to 4 messages.
assert app.conf.worker_concurrency == settings.CELERY_WORKER_CONCURRENCY
print("OK")
"""
        )
    )


def test_broker_connection_retries_on_startup() -> None:
    """A momentary Redis outage must not crash-loop the worker — that
    would defeat the durability work it is meant to serve."""
    _assert_ok(
        _run(
            """
assert app.conf.broker_connection_retry_on_startup is True
print("OK")
"""
        )
    )


def test_outbox_tasks_are_registered_and_routed_to_maintenance() -> None:
    _assert_ok(
        _run(
            """
assert app.conf.task_routes[OUTBOX_DRAIN] == {"queue": "maintenance"}
assert app.conf.task_routes[OUTBOX_RECONCILE] == {"queue": "maintenance"}
assert "app.workers.tasks_outbox" in app.conf.include

celery_app = app  # `import app.workers...` below rebinds the name `app`
import app.workers.tasks_outbox  # noqa: F401,E402
assert OUTBOX_DRAIN in celery_app.tasks
assert OUTBOX_RECONCILE in celery_app.tasks
print("OK")
"""
        )
    )


def test_child_recycling_still_bounds_worker_memory() -> None:
    """The H1 settings must not have displaced the 2026-08-03 leak
    hardening — both matter, and `acks_late` interacts with recycling
    (a recycled child's task is redelivered, not lost)."""
    _assert_ok(
        _run(
            """
assert app.conf.worker_max_tasks_per_child == settings.CELERY_MAX_TASKS_PER_CHILD
assert app.conf.worker_max_memory_per_child == settings.CELERY_MAX_MEMORY_PER_CHILD_KB
print("OK")
"""
        )
    )
