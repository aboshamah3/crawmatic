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
SCRAPE_REDISPATCH_JOBS = "maintenance.redispatch_pending_jobs"
SCRAPE_RECONCILE_FALSE_FAILURES = "maintenance.reconcile_false_failed_targets"

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

# --- Transactional outbox (audit H1) ---
# Enqueued by the scheduler's own fixed-cadence accumulators (never by a
# domain producer — a producer writes an ``outbox_messages`` row instead);
# consumed by ``apps/workers/app/workers/tasks_outbox.py`` on the existing
# ``maintenance`` queue. ``OUTBOX_DRAIN`` publishes durably-recorded
# messages to Celery; ``OUTBOX_RECONCILE`` reports backlog/dead-letter
# health and ages out terminal rows.
OUTBOX_DRAIN = "maintenance.outbox_drain"
OUTBOX_RECONCILE = "maintenance.outbox_reconcile"

# --- Cost authorization lease sweep (EPA C3, READY-006) ---
# Enqueued by the scheduler on the existing 60s-class maintenance tick;
# consumed by ``apps/workers/app/workers/tasks_maintenance.py`` on the
# existing ``maintenance`` queue. Reaps EXPIRED reservation leases — but
# only after confirming C1's ledger holds no open operation for the
# grant, so a slow worker's money is never released out from under a
# fetch that is still in flight. Without it a crashed worker's
# reservation would hold budget until an operator noticed, which is the
# one failure mode `release()` on the dispatch paths cannot cover.
COSTAUTH_RESERVATION_SWEEP = "maintenance.costauth_reservation_sweep"

# --- Provider usage reconciliation (EPA C5, READY-005 part 2) ---
# Enqueued by the scheduler on the existing daily-cadence maintenance
# tick; consumed by ``apps/workers/app/workers/tasks_maintenance.py`` on
# the existing ``maintenance`` queue. Reconciles whatever provider usage
# has already been IMPORTED (``scripts/import_dataimpulse_usage.py`` —
# file-based, no network call, run by an operator/owner) against C1's
# ledger for the prior UTC day, per (provider, provider_account) pair,
# appending ``network_operation_settlements`` versions where bytes
# reconcile. Does not itself import anything or talk to a provider API —
# see ``app_shared.netledger.reconcile`` for why the import and the
# reconciliation are deliberately separate steps.
MAINTENANCE_RECONCILE_PROVIDER_USAGE = "maintenance.reconcile_provider_usage"

# --- Network cost rollup (EPA C6) ---
# Enqueued by the scheduler on a durable daily cadence
# (``CADENCE_COST_ROLLUP``); consumed by ``apps/workers/app/workers/
# tasks_maintenance.py`` on the existing ``maintenance`` queue. Maintains
# the bounded, durable ``network_cost_rollups``/``fleet_network_cost_
# rollups`` tables (``app_shared.netledger.rollups.run_cost_rollup``)
# that ``GET /ops/metrics``'s ``cost_rollup`` section and the tenant-
# scoped ``GET /v1/cost-rollups`` both read — neither ever aggregates the
# raw ``network_operations`` ledger synchronously on request.
MAINTENANCE_COST_ROLLUP = "maintenance.cost_rollup"

# --- Seeded-entitlement staleness refresh (EPA go-live prep, 2026-08-26) ---
# Enqueued by the scheduler on a durable 6-hourly cadence
# (``CADENCE_ENTITLEMENT_REFRESH``); consumed by ``apps/workers/app/
# workers/tasks_maintenance.py`` on the existing ``maintenance`` queue.
# Re-stamps ``workspace_entitlements.observed_at`` on the rows
# ``scripts/seed_workspace_entitlements.py`` owns — and ONLY those — so
# the placeholder evidence the C3 gate reads never ages past
# ``DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS`` and starts denying all
# paid work, while the future real SaaS->engine ingest's rows keep their
# own freshness entirely. See ``app_shared.costauth.entitlements``.
MAINTENANCE_ENTITLEMENT_REFRESH = "maintenance.entitlement_refresh"

# --- Durable proxy-breaker evaluator (EPA B1, 2026-09-03) ---
# Enqueued by the scheduler on the durable ``CADENCE_BREAKER_EVALUATE``
# cadence; consumed by ``apps/workers/app/workers/tasks_maintenance.py``
# on the existing ``maintenance`` queue.
#
# Closes a DEADLOCK. The cost gate treats breaker evidence older than
# ``DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`` (3600) as missing and
# DENIES all paid work — and until this task the only thing that ever
# refreshed ``proxy_circuit_breakers.evaluated_at`` ran *inside* the
# scraping path that same gate blocks. An idle fleet (or one already
# denied for any reason) therefore let the evidence rot, after which
# nothing could ever refresh it again: no scraping -> no evaluation ->
# stale evidence -> no scraping. Evaluating from the scheduler breaks
# the cycle, because the scheduler does not need the gate's permission
# to run. See ``app_shared.access.breaker``.
MAINTENANCE_BREAKER_EVALUATE = "maintenance.breaker_evaluate"

# --- Fleet budget cap roll-forward (EPA A4/B3, 2026-09-03) ---
# Enqueued by the scheduler on the durable
# ``CADENCE_FLEET_BUDGET_ROLLFORWARD`` cadence; consumed by
# ``apps/workers/app/workers/tasks_maintenance.py`` on the existing
# ``maintenance`` queue.
#
# Closes a DATED GAP rather than a deadlock. `fleet_cost_budgets` is the
# only fleet-wide money ceiling there is, and it was put in place by a
# one-off operator run of ``scripts/seed_fleet_budget_cap.py`` covering a
# FIXED number of months — the last of them ``2026_10``. A budget row is
# born with ``NULL`` limits, so on the first paid dispatch of the month
# after the last one seeded, ``authorize()`` materialises an uncapped row
# and the ceiling is gone with no log line, no denial and no symptom
# short of the provider bill. This task re-caps the current and next
# month every 6h, carrying the last cap forward when no explicit one is
# configured. See ``app_shared.costauth.fleet_budget_policy``.
MAINTENANCE_FLEET_BUDGET_ROLLFORWARD = "maintenance.fleet_budget_rollforward"

# --- STARTED-target reaper + hard job deadline (EPA A3/B2, 2026-09-03) ---
# Enqueued by the scheduler on the same 60s maintenance tick as
# ``SCRAPE_FINALIZE_JOBS`` (and deliberately BEFORE it, so a target the
# reaper closes out is finalizable within the same tick); consumed by
# ``apps/workers/app/workers/tasks_jobs.py`` on the existing
# ``maintenance`` queue.
#
# Closes a WEDGE. When a scrapyd container is replaced mid-job, every
# target it had claimed stays ``STARTED`` — the process that would have
# written the terminal status is gone, so nothing ever writes it,
# ``finalize_jobs`` never sees "all targets terminal", and the job dangles
# ``RUNNING`` forever while the customer's refresh silently never
# completes. Neither existing sweep covers that state:
# ``SCRAPE_RECOVER_STALLED`` owns only targets still bare ``PENDING`` and
# ``SCRAPE_REDISPATCH_JOBS`` only jobs holding ``PENDING``/``DEFERRED``
# work. See ``app_shared.jobs.reaper``.
SCRAPE_REAP_STALE_TARGETS = "maintenance.reap_stale_targets"
