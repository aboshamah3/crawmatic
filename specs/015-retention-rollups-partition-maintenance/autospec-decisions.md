# Autospec Decisions — SPEC-15 Retention, Rollups & Partition Maintenance

Doc-first auto-answers and defaults, logged per autospec hard-rule 3.

## specify

- [specify] Q: Which tables and retention windows are in scope? → A: price_observations 90d, request_attempts 90d, price_alert_events 1y, webhook_events 90d, variant_price_daily_rollups 2y (source: doc §29 Partitioning and Retention).
- [specify] Q: How is retention performed? → A: partition DROP, never bulk DELETE (source: doc §29 "Retention must be implemented as partition drop, never bulk DELETE").
- [specify] Q: Ordering between rollups and retention? → A: daily rollups for a period must be verified complete BEFORE the retention job may drop the raw partitions that feed them (source: doc §29 Maintenance-job ordering).
- [specify] Q: How do readers handle references into dropped partitions? → A: soft references (plain UUID, no FK) may dangle; readers tolerate a missing row and use denormalized current-state fields (source: doc §22 / §29).
- [specify] Q: webhook_events is listed but introduced in SPEC-16 (not yet built) — in scope now? → A: registry includes it but jobs operate only over tables that actually exist, skipping absent tables without error (source: default — reconciles §29 table list with one-spec-at-a-time build order; SPEC-16 not yet implemented).
- [specify] Q: Where do the maintenance jobs run? → A: scheduled background/worker (or scheduler) periodic jobs, no scraping, off the request path, under a system/elevated cross-workspace execution context (source: doc constitution — no Scrapy in Celery; scheduler system-session pattern; no per-request hot-row writes).
- [specify] Q: Rollup source + currency handling? → A: aggregate client price + comparable same-currency competitor prices into min/avg/max + comparable count + alert_type; exclude currency-mismatched prices; exact Decimal, finite only (source: doc §22 variant_price_daily_rollups columns + monetary correctness principle + SPEC-09 comparison surface).

## clarify

No questions relayed to the user — the ambiguity scan surfaced only points resolvable doc-first or by derivation. Integrated into spec.md ## Clarifications (Session 2026-07-06):

- [clarify] Q: By which date is a raw observation assigned to a daily rollup? → A: UTC calendar date of its partition key (scraped_at), so partition range maps 1:1 to required rollup dates for the verify-before-drop check (source: derivation from §22 partition key + FR-016).
- [clarify] Q: How far ahead must partitions be pre-created? → A: at least next month (maintain current + next) (source: doc §29 "create next month's partitions" + self-heal default).
- [clarify] Q: Which day does each rollup run process + re-run handling? → A: most recent completed UTC day, idempotent upsert on unique(workspace_id, product_variant_id, date) (source: default; §29 "create daily rollups" + §22 unique constraint).
- [clarify] Q: What does "verified complete" mean for a partition? → A: a rollup exists for every in-range UTC date that had source data; no-data dates need no rollup (source: derivation from §29 ordering guarantee).

Deferred to plan (implementation-level, not user decisions): exact scheduling/orchestration mechanism (Celery beat vs scheduler service), SPEC-09 source-table wiring for rollup aggregation, concurrency/locking mechanism for maintenance passes.
