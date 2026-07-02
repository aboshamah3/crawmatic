"""Celery application for the `worker` service.

No tasks are registered in this skeleton phase (SPEC-01) — later specs
add tasks named via ``app_shared.task_names`` constants. This module only
establishes the Celery app (broker/result-backend from ``REDIS_URL``) and
the fork-safety hook required before any DB-touching task exists
(plan.md §VIII, FR-020, FR-007).

Fork-safety: Celery's prefork pool workers are created via ``fork()``.
If a parent process had already created the lazy SQLAlchemy engine
(app_shared.database), a forked child would inherit live, shared
connections/sockets, which is unsafe. ``worker_process_init`` fires in
each forked child immediately after the fork, before any task runs, so
disposing the inherited engine there guarantees each worker process
builds its own engine/pool on first use.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app_shared.config import get_settings
from app_shared.database import dispose_engine

settings = get_settings()

app = Celery(
    "workers",
    broker=settings.REDIS_URL,
    # No result backend required for the skeleton; using the same Redis
    # instance keeps configuration minimal without expanding scope.
    backend=None,
)


@worker_process_init.connect
def _dispose_inherited_engine(**kwargs: object) -> None:
    """Dispose any DB engine inherited from the parent via fork().

    Runs in each forked worker process before it handles a task, so the
    process always builds its own lazy engine on first use instead of
    sharing connections/sockets with its parent or siblings.
    """
    dispose_engine()
