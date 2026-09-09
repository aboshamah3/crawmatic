"""`reconcile_inflight_intents` actually runs on a schedule (EPA B3, closing B2).

B2 built step 5 of the commit-before-send dispatch protocol and shipped
it unwired — its own report says so: *"Nothing schedules
`reconcile_inflight_intents` yet. It is a library function with no Celery
task or beat entry."* Until something did, the protocol was open-loop: a
worker killed between its POST and the node's answer left a
`dispatch_intents` row in `POSTED` that nothing ever settled. That row is
not confirmable (nobody asked the node) and not re-postable either, since
only `RECONCILED_MISSING` authorizes a re-POST. The intent was committed
before the send precisely so the knowledge would survive the death — with
no sweep, it survived and was never read.

Four things have to be true for that loop to close, and each is a
separate way to get it wrong:

1. a registered Celery task calls the library function (`tasks_jobs`);
2. it is routed to a queue a worker actually consumes, with a time limit
   (`celery_app`) — B4's own report records four maintenance tasks that
   are registered but routed nowhere and therefore never execute;
3. the scheduler drives it (`_DURABLE_CADENCES`);
4. it is driven **durably**. An in-process accumulator resets on process
   start, which is exactly the event this sweep exists to recover from —
   the 2026-08-15 partition-gap incident is what that failure mode looks
   like when it lands.

Subprocess-loaded per this repo's convention for anything importing a
service's top-level `app` package.
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


def _run(body: str, *, app_path: str) -> subprocess.CompletedProcess:
    preamble = f'import sys\nsys.path.insert(0, "{app_path}")\n'
    return subprocess.run(
        [sys.executable, "-c", preamble + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_the_task_calls_the_reconciler_on_the_system_session() -> None:
    """Fleet-wide, bounded, and on the BYPASSRLS sessionmaker.

    A `POSTED` row's tenant is exactly what the crashed worker did not get
    to tell anyone, so the sweep cannot be workspace-scoped — and it must
    be bounded, or one pass can outlive its own time limit.
    """
    _assert_ok(
        _run(
            """
import app.workers.tasks_jobs as tasks_jobs

calls = []


def fake_reconcile(session_factory, client, **kwargs):
    calls.append({"factory": session_factory, "client": client, **kwargs})

    class _Report:
        # R11 added `in_flight` and `ambiguous`: a pass now reports the
        # rows it deliberately declined to rule on as well as the ones it
        # settled, and the task logs all six.
        examined = confirmed = missing = unreachable = in_flight = ambiguous = 0

    return _Report()


class _Settings:
    DISPATCH_RECONCILE_LIMIT = 77
    DISPATCH_RECONCILE_MIN_AGE_SECONDS = 120
    DISPATCH_RECONCILE_ABSENCE_QUORUM = 2
    DISPATCH_RECONCILE_ABSENCE_WINDOW_SECONDS = 120
    DISPATCH_RECONCILE_ABSENCE_HORIZON_SECONDS = 86400


tasks_jobs.reconcile_inflight_intents = fake_reconcile
tasks_jobs.get_settings = lambda: _Settings()
tasks_jobs.ScrapydDispatchClient = lambda **kwargs: "client-sentinel"

tasks_jobs.reconcile_dispatch_intents()

assert len(calls) == 1, calls
call = calls[0]
assert call["factory"] is tasks_jobs.get_system_session, call["factory"]
assert call["limit"] == 77
assert call["client"] == "client-sentinel"
# R11: the absence policy travels with the pass, so one sweep runs under
# one snapshot of the four knobs rather than re-reading them per row.
assert call["settings"].DISPATCH_RECONCILE_MIN_AGE_SECONDS == 120
# No workspace/job narrowing: this is a registered system sweep.
assert "workspace_id" not in call and "scrape_job_id" not in call
print("OK")
""",
            app_path="apps/workers",
        )
    )


def test_the_task_is_routed_and_time_limited() -> None:
    """Registered is not enough — it has to land where a worker listens.

    B4 found four maintenance tasks with a `time_limit` and no
    `task_routes` entry: they fall to `task_default_queue="celery"`, which
    is not one of the five declared queues, so they are never executed by
    anything. This asserts the new task is not the fifth.
    """
    _assert_ok(
        _run(
            """
from app.workers.celery_app import app as celery_app
from app_shared.task_names import DISPATCH_RECONCILE_INTENTS

# Registration happens on import, exactly as Celery's `include=[...]`
# does it at worker startup.
import app.workers.tasks_jobs  # noqa: F401,E402

app = celery_app
assert DISPATCH_RECONCILE_INTENTS in app.tasks, sorted(app.tasks)

route = app.conf.task_routes[DISPATCH_RECONCILE_INTENTS]
assert route == {"queue": "maintenance"}, route
assert "maintenance" in set(app.conf.task_queues)
assert "maintenance" in set(app.amqp.queues), sorted(app.amqp.queues)

task = app.tasks[DISPATCH_RECONCILE_INTENTS]
assert task.time_limit == 300, task.time_limit
assert 0 < task.soft_time_limit < task.time_limit, task.soft_time_limit
print("OK")
""",
            app_path="apps/workers",
        )
    )


def test_the_scheduler_drives_it_on_the_durable_cadence() -> None:
    """Durable, with its own interval knob — not an in-process float.

    The countdown lives in `maintenance_cadences`, so a scheduler restart
    (a Railway deploy) cannot reset it, and N replicas still enqueue
    exactly once.
    """
    _assert_ok(
        _run(
            """
from app.scheduler import scheduler_app
from app_shared.config import get_settings
from app_shared.models.maintenance_cadence import (
    CADENCE_DISPATCH_RECONCILE,
    DURABLE_CADENCE_KEYS,
)

entries = {key: (attr, fn) for key, attr, fn in scheduler_app._DURABLE_CADENCES}
assert CADENCE_DISPATCH_RECONCILE in entries, sorted(entries)
attr, fn = entries[CADENCE_DISPATCH_RECONCILE]
assert attr == "DISPATCH_RECONCILE_INTERVAL_SECONDS", attr
assert fn.__name__ == "_enqueue_dispatch_reconcile", fn

# `ensure_cadence_rows` seeds from this tuple, so a cadence missing from
# it has no row to claim and silently never fires.
assert CADENCE_DISPATCH_RECONCILE in DURABLE_CADENCE_KEYS, DURABLE_CADENCE_KEYS

interval = get_settings().DISPATCH_RECONCILE_INTERVAL_SECONDS
assert 0 < interval <= 3600, interval
print("OK")
""",
            app_path="apps/scheduler",
        )
    )


def test_the_enqueue_helper_targets_the_maintenance_queue_and_swallows_errors() -> None:
    """A broker hiccup must not crash-loop the scheduler.

    The deadline is still in Postgres, unclaimed — the next tick retries
    it. Same posture as every other maintenance enqueue in this loop.
    """
    _assert_ok(
        _run(
            """
from app.scheduler import scheduler_app
from app_shared.task_names import DISPATCH_RECONCILE_INTENTS

calls = []
scheduler_app.enqueue = lambda name, *, queue=None, kwargs=None: calls.append(
    (name, queue)
)
scheduler_app._enqueue_dispatch_reconcile()
assert calls == [(DISPATCH_RECONCILE_INTENTS, "maintenance")], calls


def _boom(*args, **kwargs):
    raise RuntimeError("broker down")


scheduler_app.enqueue = _boom
scheduler_app._enqueue_dispatch_reconcile()  # must not raise
print("OK")
""",
            app_path="apps/scheduler",
        )
    )
