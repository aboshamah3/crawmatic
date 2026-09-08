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

Production-config gate (audit §L1): ``_assert_production_safe_on_worker_start``
runs `app_shared.config_validation.assert_production_safe` on
``worker_init`` — see that function's docstring for why it is a signal
rather than an import-time call (this module is imported by every task
module, and by the test suite), and why it converts the failure into
``SystemExit`` (Celery's signal dispatcher swallows plain ``Exception``s).
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_ready, worker_shutdown

from app_shared.config import get_settings
from app_shared.config_validation import ProductionConfigError, assert_production_safe
from app_shared.database import dispose_engine
from app_shared.heartbeat import HeartbeatEmitter, PeriodicHeartbeat, default_instance_id
from app_shared.memory_watchdog import start_memory_watchdog
from app_shared.redis_client import get_redis_client
from app_shared.task_names import (
    COSTAUTH_RESERVATION_SWEEP,
    CREATE_WEBHOOK_EVENT,
    DISPATCH_RECONCILE_INTENTS,
    MAINTENANCE_BREAKER_EVALUATE,
    MAINTENANCE_COST_ROLLUP,
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_ENTITLEMENT_REFRESH,
    MAINTENANCE_FLEET_BUDGET_ROLLFORWARD,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RECONCILE_PROVIDER_USAGE,
    MAINTENANCE_RETENTION_DROP,
    OUTBOX_DRAIN,
    OUTBOX_RECONCILE,
    PRICE_ANALYSIS_RECOMPUTE,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_REAP_STALE_TARGETS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_RECONCILE_FALSE_FAILURES,
    SCRAPE_REDISPATCH_JOBS,
    STRATEGY_DISCOVERY_RUN,
    STRATEGY_DISCOVERY_SCAN,
    STRATEGY_LIGHT_RECHECK,
    STRATEGY_PATTERN_BACKFILL,
    STRATEGY_STATS_FLUSH,
)

logger = logging.getLogger(__name__)

#: The parent process's own heartbeat, started on `worker_ready` and
#: stopped on `worker_shutdown`. Module-level (rather than passed
#: between the two signal receivers) because Celery gives the shutdown
#: signal a different sender than the ready signal, so there is no
#: object to hang it off.
_worker_heartbeat: PeriodicHeartbeat | None = None

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
    # EPA A3/B2: the STARTED-target reaper + hard job deadline. Routed
    # explicitly for the same reason as every sweep above — a call that
    # omits `queue=` still has to land where a consumer is listening.
    SCRAPE_REAP_STALE_TARGETS: {"queue": "maintenance"},
    SCRAPE_FINALIZE_JOBS: {"queue": "maintenance"},
    SCRAPE_REDISPATCH_JOBS: {"queue": "maintenance"},
    SCRAPE_RECONCILE_FALSE_FAILURES: {"queue": "maintenance"},
    # EPA B3 (closing B2's owed wiring): step 5 of the commit-before-send
    # dispatch protocol. An ordinary `maintenance` sweep — a bounded scan
    # of POSTED intents plus one `listjobs` call per distinct node.
    DISPATCH_RECONCILE_INTENTS: {"queue": "maintenance"},
    PRICE_ANALYSIS_RECOMPUTE: {"queue": "price_analysis"},
    STRATEGY_DISCOVERY_RUN: {"queue": "strategy_discovery"},
    STRATEGY_LIGHT_RECHECK: {"queue": "maintenance"},
    STRATEGY_STATS_FLUSH: {"queue": "maintenance"},
    STRATEGY_PATTERN_BACKFILL: {"queue": "maintenance"},
    MAINTENANCE_PARTITION_CREATE: {"queue": "maintenance"},
    # EPA B1: the durable breaker evaluator. Routed explicitly so a call
    # that omits `queue=` (a runbook `.delay()`, say) still lands where a
    # consumer is listening rather than on an unconsumed default queue.
    MAINTENANCE_BREAKER_EVALUATE: {"queue": "maintenance"},
    MAINTENANCE_DAILY_ROLLUP: {"queue": "maintenance"},
    # EPA A4/B3: the fleet budget cap roll-forward. Routed explicitly for
    # the same reason as every sweep above — a call that omits `queue=`
    # still has to land where a consumer is listening.
    MAINTENANCE_FLEET_BUDGET_ROLLFORWARD: {"queue": "maintenance"},
    MAINTENANCE_RETENTION_DROP: {"queue": "maintenance"},
    CREATE_WEBHOOK_EVENT: {"queue": "webhook_events"},
    # Audit H1: both outbox passes are ordinary `maintenance` sweeps —
    # bounded DB work on the BYPASSRLS system session, no blocking fetch.
    OUTBOX_DRAIN: {"queue": "maintenance"},
    OUTBOX_RECONCILE: {"queue": "maintenance"},
    # EPA B4 (F09): the fleet-wide, chunked discovery re-drive sweep — a
    # bounded DB scan + outbox writes, no blocking fetch, the same shape
    # as its `STRATEGY_PATTERN_BACKFILL` sibling above.
    STRATEGY_DISCOVERY_SCAN: {"queue": "maintenance"},
}

# --- Per-task time limits (EPA B4, F09) ------------------------------------
#
# `CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS` (3600, see above) is sized
# from the worst-case measured task, but nothing previously enforced that
# any *individual* task actually stays under it — a runaway task (a bug,
# a hung socket a lower-level timeout failed to catch) could run past the
# visibility timeout, get redelivered to a second worker while the first
# is still executing it, and run twice. `time_limit` is the hard ceiling
# Celery SIGKILLs the task's process at; `soft_time_limit` (a grace
# window before the hard kill) raises `SoftTimeLimitExceeded` inside the
# task first, so a task with cleanup to do gets a chance to run it.
#
# Centralised here via `task_annotations` (rather than a `time_limit=`
# kwarg on each `@app.task(...)` decorator scattered across five
# `tasks_*.py` modules) so this file is the one place the whole fleet's
# time budget is visible and reviewable — and so `tests/unit/
# test_celery_time_limits.py` can assert "every registered task has a
# time_limit below the visibility timeout" as an invariant a future task
# addition cannot silently violate: a name missing from this dict has no
# `time_limit` at all (Celery's default), which that test catches.
#
# Buckets follow the plan's own groupings; `soft_time_limit` is 90% of
# `time_limit` (a design choice — only the `time_limit` values below are
# acceptance-critical), floored at a 15s grace window:
#
#   dispatch (600s)              -- one Scrapyd POST + idempotency lookup.
#   reapers/reconcilers (300s)   -- bounded per-workspace or per-key DB
#                                    sweeps; no blocking fetch.
#   breaker/webhooks (120s)      -- a handful of rows, no blocking fetch.
#   rollups (1800s per chunk)    -- cross-tenant daily aggregation.
#   discovery/analysis/backfill
#     (900s per chunk)           -- `STRATEGY_DISCOVERY_RUN`'s own probe
#                                    ladder (measured worst case 600s, see
#                                    the visibility-timeout comment above)
#                                    plus extraction/validation headroom;
#                                    `PRICE_ANALYSIS_RECOMPUTE` and
#                                    `STRATEGY_PATTERN_BACKFILL` bucketed
#                                    alongside it as the next-heaviest
#                                    shape (bounded batch, may itself
#                                    enqueue discovery runs).
_DISPATCH_LIMITS = {"time_limit": 600, "soft_time_limit": 540}
_REAPER_LIMITS = {"time_limit": 300, "soft_time_limit": 270}
_SHORT_LIMITS = {"time_limit": 120, "soft_time_limit": 100}
_ROLLUP_LIMITS = {"time_limit": 1800, "soft_time_limit": 1620}
_DISCOVERY_LIMITS = {"time_limit": 900, "soft_time_limit": 810}

app.conf.task_annotations = {
    # --- dispatch (600s) ---
    SCRAPE_DISPATCH_JOB: _DISPATCH_LIMITS,
    # The thin Scrapyd-dispatch task (`app.workers.tasks_dispatch`) is not
    # in `task_names.py` (it predates that convention) and is never
    # `include=`d directly — it registers on `app.tasks` transitively
    # because `tasks_jobs.py` (which IS `include=`d) imports it. Named
    # here by its literal string for that reason.
    "dispatch.generic_price_spider": _DISPATCH_LIMITS,
    # --- reapers/reconcilers (300s) ---
    SCRAPE_RECOVER_STALLED: _REAPER_LIMITS,
    SCRAPE_FINALIZE_JOBS: _REAPER_LIMITS,
    SCRAPE_REDISPATCH_JOBS: _REAPER_LIMITS,
    SCRAPE_RECONCILE_FALSE_FAILURES: _REAPER_LIMITS,
    SCRAPE_REAP_STALE_TARGETS: _REAPER_LIMITS,
    DISPATCH_RECONCILE_INTENTS: _REAPER_LIMITS,
    MAINTENANCE_PARTITION_CREATE: _REAPER_LIMITS,
    MAINTENANCE_RETENTION_DROP: _REAPER_LIMITS,
    MAINTENANCE_FLEET_BUDGET_ROLLFORWARD: _REAPER_LIMITS,
    STRATEGY_LIGHT_RECHECK: _REAPER_LIMITS,
    STRATEGY_STATS_FLUSH: _REAPER_LIMITS,
    STRATEGY_DISCOVERY_SCAN: _REAPER_LIMITS,
    OUTBOX_DRAIN: _REAPER_LIMITS,
    OUTBOX_RECONCILE: _REAPER_LIMITS,
    # The remaining `maintenance`-queue tasks below have no `task_routes`
    # entry of their own (a pre-existing gap predating this task — see
    # B4's report) and so are not wired to any worker queue today, but
    # every one of them IS registered on `app.tasks` (their modules are
    # `include=`d) and therefore still needs a `time_limit`, per this
    # file's own invariant above.
    COSTAUTH_RESERVATION_SWEEP: _REAPER_LIMITS,
    MAINTENANCE_RECONCILE_PROVIDER_USAGE: _REAPER_LIMITS,
    MAINTENANCE_ENTITLEMENT_REFRESH: _REAPER_LIMITS,
    # --- breaker / webhooks (120s) ---
    MAINTENANCE_BREAKER_EVALUATE: _SHORT_LIMITS,
    CREATE_WEBHOOK_EVENT: _SHORT_LIMITS,
    # --- rollups (1800s per chunk) ---
    MAINTENANCE_DAILY_ROLLUP: _ROLLUP_LIMITS,
    MAINTENANCE_COST_ROLLUP: _ROLLUP_LIMITS,
    # --- discovery / analysis / backfill (900s per chunk) ---
    STRATEGY_DISCOVERY_RUN: _DISCOVERY_LIMITS,
    PRICE_ANALYSIS_RECOMPUTE: _DISCOVERY_LIMITS,
    STRATEGY_PATTERN_BACKFILL: _DISCOVERY_LIMITS,
}


@worker_init.connect
def _assert_production_safe_on_worker_start(**kwargs: object) -> None:
    """Audit §L1: refuse to start a worker on local-dev-shaped production config.

    **Why a signal and not module import time.** `apps/api` calls
    `assert_production_safe()` at import, which is right for it: importing
    `app.main` *is* starting the API. This module is different — every
    `app.workers.tasks_*` module imports it to get the `app` object, and
    those modules are imported by the unit suite, by `celery inspect`-style
    tooling, and by anything that merely wants a task's name. An
    import-time assertion would therefore fire in contexts that are not
    starting a worker at all. ``worker_init`` fires exactly once, in the
    long-lived worker parent process, before the pool is created and
    before any task is consumed — the same lifecycle point
    `_start_memory_watchdog` (below) already uses for "runs once per
    worker boot, in the parent". Registered *first* so an unsafe deploy
    dies before it spawns a watchdog thread or forks any child.

    **Why `SystemExit` and not just letting the error propagate.** Celery's
    `Signal.send` wraps every receiver in `except Exception` and returns
    the exception as that receiver's *response* rather than re-raising
    (`celery/utils/dispatch/signal.py` — its own docstring notes `send`
    and `send_robust` do the same thing). A raised
    `ProductionConfigError` (a `RuntimeError`) would therefore be
    swallowed and the worker would boot anyway, which is the opposite of
    fail-fast. `SystemExit` derives from `BaseException`, so it slips past
    that `except Exception` and aborts `WorkController.__init__`. The
    findings are logged at CRITICAL first, because `SystemExit`'s message
    is the only thing the operator would otherwise see.
    """
    try:
        assert_production_safe()
    except ProductionConfigError as exc:
        logger.critical("worker refusing to start: %s", exc)
        raise SystemExit(1) from exc


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


@worker_ready.connect
def _start_worker_pool_heartbeat(sender: object = None, **kwargs: object) -> None:
    """`heartbeat:worker:<pool-node>` every 30s (EPA B9, F22).

    One heartbeat per **consumer pool**, not per container: `start.sh`
    (EPA B4, F09) runs two `celery worker` processes in the same service,
    `-n critical@%h` and `-n bulk@%h`, and a stuck `bulk@` pool with a
    healthy `critical@` is exactly the failure this signal exists to
    make visible. `sender` here is the pool's own `Consumer`, whose
    `hostname` IS that `-n` node name, so the two pools land on two
    distinct `heartbeat:worker:*` keys with independent fences.

    **Why `worker_ready` and not `worker_init`.** `worker_init` fires
    before the prefork pool is created, so a thread started there would
    be inherited by every forked child — N processes beating under one
    instance id, each with a copy of a Redis connection made before the
    fork. `worker_ready` fires in the parent *after* the pool exists, so
    the thread is the parent's alone. It is also the point at which the
    process is genuinely ready to consume, which is what the beat claims.

    Best-effort, exactly like `_start_memory_watchdog` above and the
    Scrapyd nodes' `_start_scraper_heartbeat`: Redis being briefly
    unreachable at boot must never keep a worker from consuming work.
    A monitoring outage is not a processing outage.
    """
    global _worker_heartbeat
    if _worker_heartbeat is not None:  # already beating (signal re-delivered)
        return
    try:
        instance_id = str(getattr(sender, "hostname", "") or "") or default_instance_id()
        _worker_heartbeat = PeriodicHeartbeat(
            HeartbeatEmitter(get_redis_client(), service="worker", instance_id=instance_id)
        ).start()
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        _worker_heartbeat = None
        logger.warning("worker heartbeat did not start", exc_info=True)


@worker_shutdown.connect
def _stop_worker_pool_heartbeat(**kwargs: object) -> None:
    """Stop the pool heartbeat thread on a graceful shutdown.

    Not required for correctness — `HEARTBEAT_TTL_SECONDS` ages out a
    beat that stops arriving, and the thread is a daemon — but stopping
    it here means a pool that is deliberately being drained stops
    claiming to be alive immediately rather than for one more TTL.
    """
    global _worker_heartbeat
    heartbeat, _worker_heartbeat = _worker_heartbeat, None
    if heartbeat is None:
        return
    try:
        heartbeat.stop()
    except Exception:  # noqa: BLE001 - shutdown path, never raise
        logger.warning("worker heartbeat did not stop cleanly", exc_info=True)
