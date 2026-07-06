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
