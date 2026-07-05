# Contract: Rediscovery — pure evaluator + triggers (FR-020, FR-021, US4)

**Pure evaluator**: `app_shared/strategy/rediscovery.py::evaluate_rediscovery(profile, combined_stats,
thresholds) -> RediscoveryDecision`. Deterministic, exhaustively unit-tested on boundary values
(Constitution testing gate).

## Trigger conditions (FR-020) — any one fires

| # | Condition | Source signal |
|---|-----------|---------------|
| 1 | `recent_failure_count ≥ STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES` (3) | profile field, ++ on preferred-method failure, reset on qualifying success (Clarification #2) |
| 2 | preferred-method cumulative `success_rate < STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR` (0.80) | persisted `strategy_attempt_stats.success_rate` **+ pending buffered deltas** (Clarification #2, FR-024) |
| 3 | selector returns empty repeatedly | repeated `PRICE_NOT_FOUND`/`SELECTOR_BROKEN` on the preferred extraction method |
| 4 | price confidence `< STRATEGY_REDISCOVERY_LOW_CONFIDENCE` (0.75) repeatedly | `avg_confidence` / recent low-confidence run |
| 5 | repeated `403`/`429` | `ScrapeErrorCode.HTTP_403` / `HTTP_429` on the preferred access method |
| 6 | currency disappears | required currency now absent on the preferred method |
| 7 | price values become unrealistic | validation-rule (§18) rejections |
| 8 | template appears changed | derived pattern for observed URLs no longer matches the profile pattern |

Returns `RediscoveryDecision(trigger: bool, reason: str | None)`. "Repeatedly" is bounded by the same
consecutive-failure/rolling counters (no single blip triggers).

## Apply (on `trigger = True`)

1. Set the profile `status = DEGRADED` (guarded `UPDATE … WHERE id = :pid AND status = 'ACTIVE'` — US4
   AS1; a `DISABLED` profile is never rediscovered).
2. Enqueue `STRATEGY_DISCOVERY_RUN` on the `strategy_discovery` queue via `app_shared.messaging.enqueue`
   for this `(workspace, competitor, domain, url_pattern)` (SC-004: degraded + enqueued within one
   evaluation cycle).
3. Record `last_failed_at`; emit a structured `strategy_rediscovery_triggered` log/metric
   (Constitution §31, `contracts/observability.md`).

## Call sites

- **Inline**: the `STRATEGY_STATS_FLUSH` task evaluates each just-flushed profile with combined counts
  (degradation observed during scraping — US4 AS1/AS2).
- **Periodic light re-check** `STRATEGY_LIGHT_RECHECK` (`maintenance` queue, §28): scans `ACTIVE`
  profiles (workspace-scoped, batched) and evaluates rediscovery **without** a full failed batch (FR-021,
  US4 AS4). Enqueued on a schedule by the scheduler service.

Healthy signals (rate ≥ 0.80, no consecutive failures, confidence ≥ 0.75) → `trigger = False`; the
profile stays `ACTIVE` (US4 "healthy signals do not trigger it").
