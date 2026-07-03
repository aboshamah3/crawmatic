# Autospec Decisions — SPEC-09 Current Prices & Alert Logic

Log of auto-answered questions and informed defaults. Format:
`- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Which tables are new vs already built? → A: New = variant_price_states, variant_alert_states, price_alert_events; already built (SPEC-07) = price_observations, match_current_prices, request_attempts (verified in libs/shared/app_shared/models/observations.py) (source: doc §22 + codebase inventory).
- [specify] Q: Exact alert decision tree + boundaries + severity mapping? → A: Ordered §23 tree; Decimal quantize 4dp ROUND_HALF_UP before compare; exactly 0%/<1%→CLOSE_TO_COMPETITORS, exactly 1%/5%→NORMAL, >5%→CHANCE_TO_INCREASE_PRICE; severity map fixed (RISK=CRITICAL etc.) (source: doc §23).
- [specify] Q: Currency handling? → A: include competitor only if success+comparable+currency==client+price not null; mismatch → exclude, mark comparable=false, store CURRENCY_MISMATCH; no FX in v1 (source: doc §19, §23).
- [specify] Q: price_analysis execution model? → A: Celery task on its own queue, separate from spider/reactor, per-variant, idempotent, deduplicated per variant per job (source: doc §25 Job Flow, §26 price_analysis queue).
- [specify] Q: Recompute triggers? → A: scrape completion (dedup per variant per job), client price/currency change (immediate, no scrape wait), match archived/paused (source: doc §23 recompute triggers).
- [specify] Q: price_alert_events partitioning? → A: monthly by created_at from birth, PK includes partition key, per §22 partitioned-table rules + SPEC-07 price_observations precedent (source: doc §22).
- [specify] Q: Which endpoints are binding vs deferred? → A: Binding: GET variants/{id}/price-comparison, GET alerts/current(+/{variant_id}), GET alert-events. Deferred: product-level comparison, matches/{id}/current-price, observations list, PATCH alerts/current (acknowledge) — not required by acceptance (source: doc §24 endpoint list + roadmap acceptance).
- [specify] Q: WebhookEvent emission (shown in §25 flow at end of price_analysis)? → A: OUT OF SCOPE — webhooks are SPEC-16; SPEC-09 stops at price_alert_events (source: doc §35 roadmap "16 Webhook Events").
- [specify] Q: Live infra verification approach? → A: exhaustive unit tests for the pure decision-tree/currency/event-transition/dedup logic + skip-clean integration tests (no Docker/Postgres/Redis/Celery/Scrapyd in build env), consistent with SPEC-01..08 (source: project convention).
