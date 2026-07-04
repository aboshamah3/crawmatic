"""Celery fork-safety hook assertion test (SPEC-08 T019, FR-016, D7).

`apps/workers/app/workers/celery_app.py` already connects
`worker_process_init` -> `dispose_engine` from SPEC-01
(`tests/unit/test_engine_hygiene.py` owns that original assertion).
SPEC-08 is the first feature whose Celery tasks (`dispatch_job`,
`finalize_jobs`, `refresh_job_counters`, `recover_stalled_batches`, all
in `app.workers.tasks_jobs`) actually touch the DB, so this test proves
the existing hook is **relied upon, not re-implemented**, alongside the
new `scrape_dispatch`/`maintenance` queues + task routes + `tasks_jobs`
`include` wiring (T012) that those tasks need to be discoverable.

Loaded in a fresh subprocess (mirrors
`tests/unit/test_engine_hygiene.py`'s `_CELERY_HOOK_WIRING_CHECK`) for
the same two reasons: (1) several app members ship their own top-level
``app`` package, so a plain ``import app.workers.celery_app`` in the
test process is ambiguous; (2) ``celery_app.py`` calls ``get_settings()``
at module scope, which needs the required env vars — a subprocess
supplies a clean, self-contained environment without polluting the
shared ``get_settings()`` lru_cache used by other tests in this process,
and (crucially here) avoids accumulating duplicate
``worker_process_init`` receivers across repeated in-process reloads.
"""

from __future__ import annotations

import os
import subprocess
import sys

_JOBS_FORK_SAFETY_CHECK = """
import inspect
import sys
import weakref

sys.path.insert(0, "apps/workers/app")
import workers.celery_app as celery_app_module

from app_shared.database import dispose_engine
from app_shared.task_names import (
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
)
from celery.signals import worker_process_init

# --- the existing SPEC-01 hook is relied upon, not re-implemented -----------

if celery_app_module.dispose_engine is not dispose_engine:
    print("NOT_SAME_DISPOSE_ENGINE")
    sys.exit(1)

source = inspect.getsource(celery_app_module)
if source.count("worker_process_init.connect") != 1:
    print("HOOK_COUNT_MISMATCH:" + str(source.count("worker_process_init.connect")))
    sys.exit(1)

handler_source = inspect.getsource(celery_app_module._dispose_inherited_engine)
if "dispose_engine" not in handler_source:
    print("HANDLER_DOES_NOT_CALL_DISPOSE_ENGINE")
    sys.exit(1)

connected_names = []
for _id, ref in worker_process_init.receivers:
    receiver = ref() if isinstance(ref, weakref.ReferenceType) else ref
    if receiver is not None:
        connected_names.append(getattr(receiver, "__name__", ""))

if "_dispose_inherited_engine" not in connected_names:
    print("HANDLER_NOT_CONNECTED_TO_SIGNAL:" + ",".join(connected_names))
    sys.exit(1)

# --- T012: scrape_dispatch/maintenance queues + routes + tasks_jobs include -

queue_names = set(celery_app_module.app.amqp.queues.keys())
if not {"scrape_dispatch", "maintenance"}.issubset(queue_names):
    print("QUEUES_MISSING:" + ",".join(sorted(queue_names)))
    sys.exit(1)

routes = celery_app_module.app.conf.task_routes
if routes.get(SCRAPE_DISPATCH_JOB, {}).get("queue") != "scrape_dispatch":
    print("DISPATCH_ROUTE_WRONG")
    sys.exit(1)
if routes.get(SCRAPE_RECOVER_STALLED, {}).get("queue") != "maintenance":
    print("RECOVER_ROUTE_WRONG")
    sys.exit(1)
if routes.get(SCRAPE_FINALIZE_JOBS, {}).get("queue") != "maintenance":
    print("FINALIZE_ROUTE_WRONG")
    sys.exit(1)

if "app.workers.tasks_jobs" not in (celery_app_module.app.conf.include or []):
    print("TASKS_JOBS_NOT_INCLUDED")
    sys.exit(1)

sys.exit(0)
"""

_JOBS_FORK_SAFETY_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_celery_app_relies_on_existing_fork_safety_hook_and_wires_jobs_queues() -> None:
    """apps/workers celery_app.py still wires worker_process_init ->
    dispose_engine (not re-implemented) AND registers the SPEC-08
    scrape_dispatch/maintenance queues + routes + tasks_jobs include."""
    env = {**os.environ, **_JOBS_FORK_SAFETY_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _JOBS_FORK_SAFETY_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
