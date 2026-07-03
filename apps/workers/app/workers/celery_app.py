"""Celery application for the `worker` service.

SPEC-08 (FR-011, FR-015, FR-016, D7) registers the first DB-touching
tasks — ``dispatch_job`` (``scrape_dispatch`` queue) and
``finalize_jobs``/``refresh_job_counters``/``recover_stalled_batches``
(``maintenance`` queue), both in ``app.workers.tasks_jobs`` — plus the
queues/routes they run on. This module only establishes the Celery app
(broker/result-backend from ``REDIS_URL``), the queue/route wiring, and
the fork-safety hook required before any DB-touching task exists
(plan.md §VIII, FR-020, FR-007).

Fork-safety: Celery's prefork pool workers are created via ``fork()``.
If a parent process had already created the lazy SQLAlchemy engine
(app_shared.database), a forked child would inherit live, shared
connections/sockets, which is unsafe. ``worker_process_init`` fires in
each forked child immediately after the fork, before any task runs, so
disposing the inherited engine there guarantees each worker process
builds its own engine/pool on first use. This hook already existed
(SPEC-01) and is asserted here (``test_jobs_fork_safety.py``), not
re-implemented — SPEC-08 is simply the first feature whose tasks
actually touch the DB and therefore rely on it (FR-016).
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app_shared.config import get_settings
from app_shared.database import dispose_engine
from app_shared.task_names import (
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
)

settings = get_settings()

app = Celery(
    "workers",
    broker=settings.REDIS_URL,
    # No result backend required for the skeleton; using the same Redis
    # instance keeps configuration minimal without expanding scope.
    backend=None,
    include=["app.workers.tasks_jobs"],
)

# --- Jobs & orchestration queues/routes (SPEC-08 FR-011, FR-015) -----------
#
# `scrape_dispatch` carries the dispatch-into-Scrapyd work; `maintenance`
# carries the periodic finalize/counter-refresh/stall-recovery scans. Kept
# separate from the default queue so dispatch/maintenance workers can be
# scaled and deployed independently of any other worker traffic.
app.conf.task_queues = {
    "scrape_dispatch": {},
    "maintenance": {},
}
app.conf.task_routes = {
    SCRAPE_DISPATCH_JOB: {"queue": "scrape_dispatch"},
    SCRAPE_RECOVER_STALLED: {"queue": "maintenance"},
    SCRAPE_FINALIZE_JOBS: {"queue": "maintenance"},
}


@worker_process_init.connect
def _dispose_inherited_engine(**kwargs: object) -> None:
    """Dispose any DB engine inherited from the parent via fork().

    Runs in each forked worker process before it handles a task, so the
    process always builds its own lazy engine on first use instead of
    sharing connections/sockets with its parent or siblings.
    """
    dispose_engine()
