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

## clarify

- [clarify] Q: variant_alert_states.status vocabulary? → A: ACTIVE/RESOLVED (ACTIVE while type non-NORMAL, RESOLVED when →NORMAL/NONE) (default — doc §22 lists field only; informed guess, integrated FR-013 + Assumptions).
- [clarify] Q: Persist an event on every (incl. unchanged) analysis? → A: No — only on type/severity change; unchanged advances last_seen_at, no row (source: doc §26 "must not be incremented"/§23 "created when alert changes"; integrated FR-013).
- [clarify] Q: Exact event-transition rule? → A: ordered CREATED/UPDATED/RESOLVED/REOPENED/none rule pinned in Clarifications for determinism (default — doc names event types but not the transition map; integrated FR-013 + US2 scenarios).
- [clarify] No questions relayed to user — all ambiguities resolved doc-first / by clear default; none high-impact enough to require a human decision.

## analyze

speckit-analyze: 0 CRITICAL, 0 HIGH, 1 MEDIUM, 1 LOW. No user pause (no CRITICAL). Remediations applied to artifacts:

- [analyze] U1 (MEDIUM, missing-client-price branch untested): the "null client price → non-analyzable" edge had no FR/engine branch/test. Verified product_variants.current_price is NOT NULL (SPEC-04), so it's defensive-only. → Fixed: added defensive step 0 (`client_price is None → NO_COMPETITOR_DATA`, must not raise) to FR-007, contracts/alert-engine.md `decide`, and T014; added a test bullet to T015. Documented in spec Edge Cases as a guard, not a reachable state.
- [analyze] I1 (LOW, dead severity branch): severity is a pure function of type (FR-011), so "same-type severity change → UPDATED" is unreachable via the real engine. → Documented as a defensive branch in spec Edge Cases + contracts/alert-engine.md `transition` note + T015 test bullet (exercised via hand-constructed input). No behavior change.
- [analyze] Coverage 100% (20 FR + 8 SC → ≥1 task each); 0 duplication; 0 constitution violations. Not re-running analyze: no CRITICAL/HIGH fixed.

## converge

- [converge] speckit-converge verdict: CONVERGED — no remaining work; tasks.md unchanged. Verified in-code (not just [X]): 3 tables + partitioned events + RLS + migration e4a75b48360c; pure §23 engine (steps 0–8, Decimal-quantized boundaries, severity map, currency filter, transition rule); recompute_variant upserting all 3 tables + events-only-on-change + latest_alert_state_id linkage + currency write-back; 3 triggers (SET NX dedup, immediate client-change, match archive/pause/delete); 4 alerts:read endpoints. Only unchecked items are the deferred skip-clean live integration tests (T034–T039).
- [converge] Final gate: 1186 unit passed; integration 3 passed/165 skipped/0 errors; single head e4a75b48360c; scoping guard clean.
