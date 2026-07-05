# Contract: Rediscovery — pure evaluator + triggers (FR-020, FR-021, US4)

**Pure evaluator**: `app_shared/strategy/rediscovery.py::evaluate_rediscovery(profile, combined_stats,
recent_signals, thresholds) -> RediscoveryDecision`. Deterministic, exhaustively unit-tested on boundary
values (Constitution testing gate). `combined_stats` carries the aggregate counters (persisted +
pending); `recent_signals` carries the recent per-attempt outcomes for the preferred method (see below).

## Two signal sources (FR-020a) — no hot-path schema widening

- **Aggregate/counter** (conditions 1, 2): `profile.recent_failure_count` and the rolled-up per-method
  `strategy_attempt_stats` **+ pending buffered deltas**. Fed via `combined_stats`.
- **Per-attempt outcome** (conditions 3, 5, 6, 7, 8): assembled off the hot path by the flush task /
  periodic light re-check from the recent `request_attempts` rows for the profile's preferred access +
  extraction method (`RecentSignals`: last-N consecutive `error_code`, HTTP status, extracted price,
  currency-present flag, confidence, observed URL). The buffered-stats recorder (`contracts/stats-buffer.md`)
  is **NOT** widened — it stays success/failure/rt/confidence/URL only.

## Trigger conditions (FR-020) — any one fires

| # | Condition | Source signal |
|---|-----------|---------------|
| 1 | `recent_failure_count ≥ STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES` (3) | `combined_stats` — profile field, ++ on preferred-method failure, reset on qualifying success (Clarification #2) |
| 2 | preferred-method cumulative `success_rate < STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR` (0.80) | `combined_stats` — persisted `strategy_attempt_stats.success_rate` **+ pending buffered deltas** (Clarification #2, FR-024) |
| 3 | selector returns empty ≥ threshold (default 3) consecutive | `recent_signals` — consecutive `PRICE_NOT_FOUND`/`SELECTOR_BROKEN` on the preferred extraction method |
| 4 | price confidence `< STRATEGY_REDISCOVERY_LOW_CONFIDENCE` (0.75) ≥ threshold consecutive | `recent_signals` confidence (and `combined_stats.avg_confidence`) |
| 5 | `403`/`429` ≥ threshold consecutive | `recent_signals` — `ScrapeErrorCode.HTTP_403` / `HTTP_429` on the preferred access method |
| 6 | required currency absent ≥ threshold consecutive | `recent_signals` — currency-present flag false on the preferred method |
| 7 | price values become unrealistic ≥ threshold consecutive | `recent_signals` — extracted price fails §18 price-validation bounds (FR-020b) |
| 8 | template appears changed ≥ threshold consecutive | `recent_signals` — re-derived `url_pattern` (current `URL_PATTERN_ALGORITHM_VERSION`) of observed URLs ≠ profile `url_pattern` (FR-020b) |

Returns `RediscoveryDecision(trigger: bool, reason: str | None)`. Every "repeatedly" condition is bounded
by a configurable consecutive-occurrence threshold (default 3, reset on a qualifying success) — no single
blip triggers.

## Apply (on `trigger = True`)

1. Set the profile `status = DEGRADED` (guarded `UPDATE … WHERE id = :pid AND status = 'ACTIVE'` — US4
   AS1; a `DISABLED` profile is never rediscovered).
2. Enqueue `STRATEGY_DISCOVERY_RUN` on the `strategy_discovery` queue via `app_shared.messaging.enqueue`
   for this `(workspace, competitor, domain, url_pattern)` (SC-004: degraded + enqueued within one
   evaluation cycle).
3. Record `last_failed_at`; emit a structured `strategy_rediscovery_triggered` log/metric
   (Constitution §31, `contracts/observability.md`).

## Call sites

Both call sites assemble `recent_signals` for the profile's preferred method from recent
`request_attempts` (off the hot path) and pass it alongside `combined_stats`.

- **Inline**: the `STRATEGY_STATS_FLUSH` task evaluates each just-flushed profile with combined counts
  + recent signals (degradation observed during scraping — US4 AS1/AS2).
- **Periodic light re-check** `STRATEGY_LIGHT_RECHECK` (`maintenance` queue, §28): scans `ACTIVE`
  profiles (workspace-scoped, batched) and evaluates rediscovery **without** a full failed batch (FR-021,
  US4 AS4). Enqueued on a schedule by the scheduler service.

Healthy signals (rate ≥ 0.80, no consecutive failures, confidence ≥ 0.75) → `trigger = False`; the
profile stays `ACTIVE` (US4 "healthy signals do not trigger it").
