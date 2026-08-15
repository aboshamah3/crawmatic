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
from celery.signals import worker_init, worker_process_init

from app_shared.config import get_settings
from app_shared.database import dispose_engine
from app_shared.memory_watchdog import start_memory_watchdog
from app_shared.task_names import (
    CREATE_WEBHOOK_EVENT,
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RETENTION_DROP,
    OUTBOX_DRAIN,
    OUTBOX_RECONCILE,
    PRICE_ANALYSIS_RECOMPUTE,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_REDISPATCH_JOBS,
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
        "app.workers.tasks_webhooks",
        "app.workers.tasks_outbox",
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
#
# `MAINTENANCE_DAILY_ROLLUP` (SPEC-15 US2, contracts/daily-rollup.md) is
# also a `maintenance` task — aggregates that day's `price_observations`
# into `variant_price_daily_rollups` on the BYPASSRLS system session, no
# blocking fetch.
#
# `MAINTENANCE_RETENTION_DROP` (SPEC-15 US3, contracts/retention-drop.md)
# is also a `maintenance` task — drops whole expired monthly partitions
# (never bulk DELETE on a raw table) after verifying daily-rollup
# coverage for `price_observations`, plus the one sanctioned bulk DELETE
# aging `variant_price_daily_rollups`, on the BYPASSRLS system session,
# no blocking fetch.
#
# `webhook_events` (SPEC-16 FR-008/FR-009) carries `CREATE_WEBHOOK_EVENT` —
# the fire-and-forget, post-commit event-recording task enqueued by three
# existing producer seams (alert transitions, job finalization, strategy
# status changes). Kept on its own queue so it never competes with/blocks
# `maintenance`/`price_analysis` traffic; it does no blocking fetch, just
# one insert.
# Prefork pool size (PLAN_AMAZON_NOON_PRICING Phase 4a): without this,
# Celery defaults to one process per CPU core — ~48 idle processes /
# ~3.85 GB on Railway for a task mix that is entirely short DB/broker
# calls. Env-tunable via CELERY_WORKER_CONCURRENCY, no rebuild needed.
app.conf.worker_concurrency = settings.CELERY_WORKER_CONCURRENCY

# Prefork child recycling (2026-08-03 memory-leak hardening): retire each
# child after N tasks and once its resident set passes N KB. Celery lets
# the running task finish before replacing the process, so this bounds how
# long a per-child leak can accumulate without ever interrupting or
# throttling a legitimate heavy run. Both env-tunable, no rebuild needed.
app.conf.worker_max_tasks_per_child = settings.CELERY_MAX_TASKS_PER_CHILD
app.conf.worker_max_memory_per_child = settings.CELERY_MAX_MEMORY_PER_CHILD_KB

# --- Delivery reliability (2026-08-15 audit risk H1) -----------------------
#
# Before this block Celery ran on its defaults: early acknowledgement,
# no worker-loss rejection, no explicit Redis visibility timeout, and
# prefetch 4. A worker killed mid-task (OOM, container eviction, deploy)
# therefore lost the task outright — the message was acked the moment it
# was delivered, so nothing ever redelivered it.
#
# `task_acks_late`: acknowledge AFTER the task returns, so a task that
# dies with its worker is redelivered instead of vanishing. This is only
# safe because every registered task is idempotent under redelivery —
# audited task-by-task, and where a task was not idempotent it was made
# so in this same change (see each task module's docstring; the
# non-trivial ones were `create_webhook_event`, which now inserts under a
# deterministic primary key with ON CONFLICT DO NOTHING, and
# `run_discovery`, which now refuses to re-probe a run that is no longer
# PENDING — that one costs real proxy money per replay).
app.conf.task_acks_late = True

# `task_reject_on_worker_lost`: when a prefork child is killed (SIGKILL /
# OOM killer / `worker_max_memory_per_child` overshoot), requeue the task
# rather than marking it failed and dropping it. Only meaningful together
# with `task_acks_late` above.
app.conf.task_reject_on_worker_lost = True

# A task that raises is still acknowledged (Celery's default). Redelivery
# is for *lost workers*, not for application errors — an exception that
# reproduces would otherwise become an infinite poison-pill loop, and
# every task here already has its own retry/backoff or is re-driven by a
# periodic sweep.
app.conf.task_acks_on_failure_or_timeout = True

# Redis visibility timeout: how long a delivered-but-unacked message
# stays invisible before redelivery. MUST exceed the longest task
# runtime, otherwise a healthy long task is redelivered and runs twice.
# Sized from the measured worst case (STRATEGY_DISCOVERY_RUN's ~600s
# probe loop) with a 6x margin — see the settings knob's comment in
# `app_shared.config` for the arithmetic.
app.conf.broker_transport_options = {
    "visibility_timeout": settings.CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
}

# Reserve at most one extra message per child. With late acks a
# prefetched message is held unacknowledged, so hoarding multiplies what
# a dead worker strands for a full visibility timeout — and this task mix
# (seconds-to-minutes tasks vs ~1ms broker round-trips) gains nothing
# from batching. See the settings knob for the full reasoning.
app.conf.worker_prefetch_multiplier = settings.CELERY_WORKER_PREFETCH_MULTIPLIER

# Keep retrying the broker connection during startup instead of crashing
# the container when Redis is momentarily unavailable (Celery 6 makes
# this False by default; a crash-loop here would defeat the whole point
# of the durability work).
app.conf.broker_connection_retry_on_startup = True

app.conf.task_queues = {
    "scrape_dispatch": {},
    "maintenance": {},
    "price_analysis": {},
    "strategy_discovery": {},
    "webhook_events": {},
}
app.conf.task_routes = {
    SCRAPE_DISPATCH_JOB: {"queue": "scrape_dispatch"},
    SCRAPE_RECOVER_STALLED: {"queue": "maintenance"},
    SCRAPE_FINALIZE_JOBS: {"queue": "maintenance"},
    SCRAPE_REDISPATCH_JOBS: {"queue": "maintenance"},
    PRICE_ANALYSIS_RECOMPUTE: {"queue": "price_analysis"},
    STRATEGY_DISCOVERY_RUN: {"queue": "strategy_discovery"},
    STRATEGY_LIGHT_RECHECK: {"queue": "maintenance"},
    STRATEGY_STATS_FLUSH: {"queue": "maintenance"},
    STRATEGY_PATTERN_BACKFILL: {"queue": "maintenance"},
    MAINTENANCE_PARTITION_CREATE: {"queue": "maintenance"},
    MAINTENANCE_DAILY_ROLLUP: {"queue": "maintenance"},
    MAINTENANCE_RETENTION_DROP: {"queue": "maintenance"},
    CREATE_WEBHOOK_EVENT: {"queue": "webhook_events"},
    # Audit H1: both outbox passes are ordinary `maintenance` sweeps —
    # bounded DB work on the BYPASSRLS system session, no blocking fetch.
    OUTBOX_DRAIN: {"queue": "maintenance"},
    OUTBOX_RECONCILE: {"queue": "maintenance"},
}


@worker_init.connect
def _start_memory_watchdog(**kwargs: object) -> None:
    """Start the container memory watchdog in the worker's main process.

    ``worker_init`` fires once in the long-lived parent (not per forked
    child), which is exactly the process
    ``worker_max_memory_per_child`` cannot protect — see
    ``app_shared.memory_watchdog``. No-op unless
    ``WATCHDOG_MEMORY_LIMIT_MB`` is set (Railway: 2048 for this service).
    """
    start_memory_watchdog("worker")


@worker_process_init.connect
def _dispose_inherited_engine(**kwargs: object) -> None:
    """Dispose any DB engine inherited from the parent via fork().

    Runs in each forked worker process before it handles a task, so the
    process always builds its own lazy engine on first use instead of
    sharing connections/sockets with its parent or siblings.
    """
    dispose_engine()
