"""Celery application for the `worker` service.

SPEC-08 (FR-011, FR-015, FR-016, D7) registers the first DB-touching
tasks — ``dispatch_job`` (``scrape_dispatch`` queue) and
``finalize_jobs``/``refresh_job_counters``/``recover_stalled_batches``
(``maintenance`` queue), both in ``app.workers.tasks_jobs`` — plus the
queues/routes they run on. SPEC-09 (FR-012, D4) adds
``recompute_variant`` on its own ``price_analysis`` queue, in
``app.workers.tasks_analysis`` (the task module itself lands in a later
phase — its queue/route/include wiring is pre-registered here, mirroring
how SPEC-08 pre-registered ``app.workers.tasks_jobs``). This module only
establishes the Celery app (broker/result-backend from ``REDIS_URL``),
the queue/route wiring, and the fork-safety hook required before any
DB-touching task exists (plan.md §VIII, FR-020, FR-007).

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
    MAINTENANCE_PARTITION_CREATE,
    PRICE_ANALYSIS_RECOMPUTE,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    STRATEGY_DISCOVERY_RUN,
    STRATEGY_LIGHT_RECHECK,
    STRATEGY_PATTERN_BACKFILL,
    STRATEGY_STATS_FLUSH,
)

settings = get_settings()

app = Celery(
    "workers",
    broker=settings.REDIS_URL,
    # No result backend required for the skeleton; using the same Redis
    # instance keeps configuration minimal without expanding scope.
    backend=None,
    include=[
        "app.workers.tasks_jobs",
        "app.workers.tasks_analysis",
        "app.workers.tasks_strategy",
        "app.workers.tasks_maintenance",
    ],
)

# --- Jobs & orchestration queues/routes (SPEC-08 FR-011, FR-015) -----------
#
# `scrape_dispatch` carries the dispatch-into-Scrapyd work; `maintenance`
# carries the periodic finalize/counter-refresh/stall-recovery scans. Kept
# separate from the default queue so dispatch/maintenance workers can be
# scaled and deployed independently of any other worker traffic.
#
# `price_analysis` (SPEC-09 FR-012, D4) carries `recompute_variant` — kept
# on its own queue, separate from `scrape_dispatch`/`maintenance` and from
# the Scrapyd/reactor runtime (Principle V, §26), so it can be scaled and
# deployed independently.
#
# `strategy_discovery` (SPEC-12 US3, §26, contracts/discovery.md) carries
# `STRATEGY_DISCOVERY_RUN` — the one task allowed to probe multiple access
# methods on a small sample; kept on its own queue since it does its own
# blocking HTTP fetches (data-model.md §8).
#
# `STRATEGY_LIGHT_RECHECK` (SPEC-12 US4, contracts/rediscovery.md "Periodic
# light re-check", FR-021) is a `maintenance` task alongside
# `SCRAPE_FINALIZE_JOBS`/`SCRAPE_RECOVER_STALLED` — it only reads/updates
# `domain_strategy_profiles`/`strategy_attempt_stats`, no blocking fetch.
#
# `MAINTENANCE_PARTITION_CREATE` (SPEC-15 US1, contracts/partition-creation.md)
# is also a `maintenance` task — runtime `CREATE TABLE ... PARTITION OF`
# DDL + catalog reads on the BYPASSRLS system session, no blocking fetch.
app.conf.task_queues = {
    "scrape_dispatch": {},
    "maintenance": {},
    "price_analysis": {},
    "strategy_discovery": {},
}
app.conf.task_routes = {
    SCRAPE_DISPATCH_JOB: {"queue": "scrape_dispatch"},
    SCRAPE_RECOVER_STALLED: {"queue": "maintenance"},
    SCRAPE_FINALIZE_JOBS: {"queue": "maintenance"},
    PRICE_ANALYSIS_RECOMPUTE: {"queue": "price_analysis"},
    STRATEGY_DISCOVERY_RUN: {"queue": "strategy_discovery"},
    STRATEGY_LIGHT_RECHECK: {"queue": "maintenance"},
    STRATEGY_STATS_FLUSH: {"queue": "maintenance"},
    STRATEGY_PATTERN_BACKFILL: {"queue": "maintenance"},
    MAINTENANCE_PARTITION_CREATE: {"queue": "maintenance"},
}


@worker_process_init.connect
def _dispose_inherited_engine(**kwargs: object) -> None:
    """Dispose any DB engine inherited from the parent via fork().

    Runs in each forked worker process before it handles a task, so the
    process always builds its own lazy engine on first use instead of
    sharing connections/sockets with its parent or siblings.
    """
    dispose_engine()
