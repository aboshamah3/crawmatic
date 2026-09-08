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
# EPA B3 (2026-09-07), closing B2's owed wiring. Step 5 of B2's
# commit-before-send dispatch protocol: settle every ``POSTED``
# ``dispatch_intents`` row against the node it names
# (``app_shared.jobs.dispatch_intents.reconcile_inflight_intents``). B2
# shipped that function with no schedule at all — it deliberately stopped
# short of touching the scheduler's beat loop — so a worker killed between
# its POST and the node's answer left a ``POSTED`` row nothing ever
# settled: not confirmable, and not re-postable either (only
# ``RECONCILED_MISSING`` authorizes a re-POST). Driven by the DURABLE
# ``dispatch_reconcile`` cadence, not an in-process accumulator: the
# protocol exists to survive a process dying, so the sweep that cleans up
# after that death has to survive it too.
DISPATCH_RECONCILE_INTENTS = "maintenance.reconcile_dispatch_intents"

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

# --- Discovery fleet-wide chunked scan (EPA B4, F09) ---
# "Resumable long maintenance": bounded, cursor-driven sweep over
# `domain_strategy_profiles` rows stuck at `DISCOVERY_REQUIRED` (any
# path that reset a profile back to that status without itself
# enqueueing `STRATEGY_DISCOVERY_RUN`, e.g. a restored/imported
# dataset). Processes at most `Settings.
# STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN` profiles per invocation,
# persists how far it got in `strategy_discovery_state`, and
# re-enqueues itself to continue a still-in-progress pass -- so neither
# a task time limit nor a worker restart mid-scan loses its place or
# doubles up on already-forwarded profiles. A `maintenance`-queue task
# (bounded DB scan + outbox writes, no blocking fetch), the same queue
# as its `STRATEGY_PATTERN_BACKFILL` sibling.
STRATEGY_DISCOVERY_SCAN = "maintenance.strategy_discovery_scan"

# --- Retention, rollups & partition maintenance (SPEC-15, research R8) ---
# Enqueued via the same ``app_shared.messaging.enqueue`` producer seam by the
# scheduler's fixed-cadence accumulators; consumed by
# ``apps/workers/app/workers/tasks_maintenance.py`` on the existing
# ``maintenance`` queue.
MAINTENANCE_PARTITION_CREATE = "maintenance.partition_create"
MAINTENANCE_DAILY_ROLLUP = "maintenance.daily_rollup"
MAINTENANCE_RETENTION_DROP = "maintenance.retention_drop"

# --- Evidence-blob retention (EPA C5, F19) ---------------------------------
# The FILESYSTEM half of retention, and the only retention job in this
# system whose objects are not rows: the content-addressed raw-evidence
# store behind `price_observations.offer_raw_evidence_hash`.
#
# It is a separate task from `MAINTENANCE_RETENTION_DROP` rather than a
# step inside it because the two have opposite failure modes. A partition
# drop that does not run costs disk; a blob deletion that runs when it
# should not costs the evidence a price was ever justified by, and is
# irreversible. Keeping them apart means the gated, irreversible one can
# be paused, rate-limited or dry-run without touching the routine one.
#
# `maintenance` queue, daily (`EVIDENCE_RETENTION_INTERVAL_SECONDS`),
# consumed by `apps/workers/app/workers/tasks_maintenance.py`. Closes the
# gap `docs/RETENTION_POLICY.md` §2.1 records as "No deletion mechanism
# exists today".
MAINTENANCE_EVIDENCE_RETENTION = "maintenance.evidence_retention"

# --- Ledger child summarization (EPA C9, F14) ------------------------------
# The LEDGER half of retention, and the only retention job here that
# COMPRESSES rather than deletes: for a settled browser navigation older
# than `RETENTION_NETWORK_OPERATION_CHILDREN_DAYS`, it writes one
# `network_operation_resource_summaries` row carrying the children's
# totals and deletes the children in the same transaction
# (`app_shared.maintenance.ledger_summaries`).
#
# Its own task rather than a step inside `MAINTENANCE_RETENTION_DROP`
# for the same reason `MAINTENANCE_EVIDENCE_RETENTION` is: the partition
# drop is bounded, cheap and reversible-by-restore, while this one reads
# and rewrites a fan-out that can be hundreds of rows per parent and is
# gated on a provider settlement having ARRIVED. An operator who needs to
# pause one must not have to pause the other.
#
# `maintenance` queue, daily (`LEDGER_SUMMARIZE_INTERVAL_SECONDS`),
# consumed by `apps/workers/app/workers/tasks_maintenance.py`. Inert
# until the owner names `network_operation_children` in
# `RETENTION_ENABLED_CLASSES`.
MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN = "maintenance.ledger_summarize_children"

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

# --- Per-domain request timeout tuner (EPA C1/F08, 2026-09-07) ---
# Rewrites ``domain_rules.request_timeout_seconds`` for every domain with
# enough successful attempts to measure: ``clamp(1.5 x p95(successful
# attempt duration, 7 d), 10 s, 60 s)``. Bounds the 46.9 s average
# proxied-HTTP attempt the deep dive found -- a number that measures the
# 60 s GLOBAL default rather than any domain, because every doomed fetch
# pays the full ceiling before anyone learns anything. Reads a 7-day
# aggregate and writes at most one row per domain, so it is cheap and
# strictly idempotent within a window. See
# ``app_shared.maintenance.domain_timeouts``.
MAINTENANCE_DOMAIN_TIMEOUT_TUNE = "maintenance.domain_timeout_tune"

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

# --- Daily cost and freshness scorecard (EPA D5, deep dive §12 item 9) ---
# Writes one ``fleet_daily_scorecard`` row per UTC day (yesterday's,
# closed window) carrying the fleet's own cost/freshness figures --
# provider bytes, valid-fresh-match rate and its attempt amplification,
# browser/proxy mix, queue and persistence p95s, budget reserved/settled,
# backup egress (fed by C10's ``POST /admin/ops/backup-report``
# receiver), and cost per valid-fresh match. See
# ``app_shared.maintenance.scorecard`` for the full field-by-field
# provenance, including which three columns are always ``NULL`` today
# (no durable Railway usage-API store exists yet) and why a missing
# input is written as ``NULL``, never a fabricated ``0``.
#
# ``maintenance`` queue, daily (``SCORECARD_INTERVAL_SECONDS``), on the
# scheduler's durable cadence -- the same shape as
# ``MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN``/``MAINTENANCE_DAILY_ROLLUP``.
# Read back by ``GET /admin/scorecard?days=30``
# (``apps/api/app/routers/admin_ops.py``), same service-token guard as
# the rest of that router.
MAINTENANCE_DAILY_SCORECARD = "maintenance.daily_scorecard"
