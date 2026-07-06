"""Celery task-name string constants.

Task names live here as plain strings so any process — a Scrapyd
spider, the scheduler, the API — can enqueue work via Celery's
``send_task(name, ...)`` without importing ``apps/workers`` itself.
That indirection is the dependency boundary that keeps
scrapy/twisted/playwright out of the worker/API import closures
(Constitution V: Disciplined Scraping Runtime).

This module intentionally imports nothing from ``celery``.
"""

from __future__ import annotations

# --- Jobs & orchestration (SPEC-08 FR-011, FR-015, D8) ---
# Enqueued via ``app_shared.messaging.enqueue`` by the API/pipeline; consumed
# by ``apps/workers/app/workers/tasks_jobs.py``.
SCRAPE_DISPATCH_JOB = "scrape_dispatch.dispatch_job"
SCRAPE_RECOVER_STALLED = "maintenance.recover_stalled_batches"
SCRAPE_FINALIZE_JOBS = "maintenance.finalize_jobs"

# --- Price analysis (SPEC-09 FR-012, D4) ---
# Enqueued via ``app_shared.messaging.enqueue`` from three triggers (scrape
# completion, client price/currency change, match archive/pause); consumed
# by ``apps/workers/app/workers/tasks_analysis.py`` on its own
# ``price_analysis`` queue.
PRICE_ANALYSIS_RECOMPUTE = "price_analysis.recompute_variant"

# --- Domain strategy optimizer (SPEC-12, data-model §8) ---
# Enqueued via the same ``app_shared.messaging.enqueue`` producer seam;
# consumed by ``apps/workers/app/workers/tasks_strategy.py``.
# STRATEGY_DISCOVERY_RUN runs on its own ``strategy_discovery`` queue (§26);
# the other three run on the existing ``maintenance`` queue.
STRATEGY_DISCOVERY_RUN = "strategy_discovery.run_discovery"
STRATEGY_STATS_FLUSH = "maintenance.strategy_stats_flush"
STRATEGY_LIGHT_RECHECK = "maintenance.strategy_light_recheck"
STRATEGY_PATTERN_BACKFILL = "maintenance.strategy_pattern_backfill"

# --- Retention, rollups & partition maintenance (SPEC-15, research R8) ---
# Enqueued via the same ``app_shared.messaging.enqueue`` producer seam by the
# scheduler's fixed-cadence accumulators; consumed by
# ``apps/workers/app/workers/tasks_maintenance.py`` on the existing
# ``maintenance`` queue.
MAINTENANCE_PARTITION_CREATE = "maintenance.partition_create"
MAINTENANCE_DAILY_ROLLUP = "maintenance.daily_rollup"
MAINTENANCE_RETENTION_DROP = "maintenance.retention_drop"

# --- Webhook events (SPEC-16 FR-008, FR-009) ---
# Enqueued via the same ``app_shared.messaging.enqueue`` producer seam by
# three existing sources (alert transitions, job finalization, strategy
# status changes) strictly after their own commit; consumed by
# ``apps/workers/app/workers/tasks_webhooks.py`` on the new
# ``webhook_events`` queue.
CREATE_WEBHOOK_EVENT = "webhook_events.create_webhook_event"
