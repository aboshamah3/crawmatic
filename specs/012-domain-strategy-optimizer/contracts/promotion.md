# Contract: Promotion — pure evaluator + guarded apply (FR-010, FR-011, US1)

**Pure evaluator**: `app_shared/strategy/promotion.py::evaluate_promotion(combined, distinct_url_count,
thresholds) -> PromotionDecision`. Framework-agnostic, deterministic, exhaustively unit-tested.

## Qualifying success (gated at record time, `contracts/stats-buffer.md`)

A success counts toward promotion **only if** all hold (FR-010, US1 AS3):
- `confidence ≥ STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD` (default 0.85),
- price is a valid numeric `Decimal` (Constitution VII, `app_shared.money`),
- currency is valid **when required** (currency-required + absent ⇒ not qualifying; Edge Cases).

Non-qualifying successes still `HINCRBY success` (for `success_rate`) but are **not** `SADD`-ed to the
distinct-URL SET and do not increment the qualifying tally — so they cannot drive promotion.

## `evaluate_promotion` inputs / output

- `combined`: persisted `success_count` + pending `success` delta for the method (the *qualifying*
  count is tracked separately; see note) — plus the method's confidence average.
- `distinct_url_count`: `SCARD straturl:{profile}:{method_type}:{method_name}` (only qualifying URLs).
- `thresholds`: `min_successes` (3), `min_distinct_urls` (3), `confidence_threshold` (0.85).

Returns `PromotionDecision(promote: bool, confidence: Decimal | None, reason: str)`:
`promote = qualifying_success_count ≥ min_successes AND distinct_url_count ≥ min_distinct_urls`.

- US1 AS1: 3 qualifying successes across ≥3 URLs → `promote = True`.
- US1 AS2: 3 successes but only 2 distinct URLs → `promote = False` (distinct-URL gate).
- US1 AS3: a low-confidence / invalid-price / missing-required-currency success never entered the
  qualifying count → does not push toward promotion.

> Note: "qualifying success count" is tracked as its own buffered counter (or derived as the SET is only
> populated by qualifying successes and one URL yields ≥1 success) — implementation keeps a
> `HINCRBY qual_success` field so the count and the distinct-URL SCARD are independently checkable.

## Apply (in the flush task, `contracts/stats-buffer.md` step 4)

Access and extraction are evaluated **separately** (FR-011, US1 AS5). On a qualifying access method:
set `preferred_access_method` + `access_confidence`. On a qualifying extraction method: set
`preferred_extraction_method` + `extraction_confidence`. Then bump `confirmed_success_count` and move the
profile to `ACTIVE`.

**Concurrency guard** (Edge Cases "Concurrent promotion"): the write is one atomic statement
```sql
UPDATE domain_strategy_profiles
   SET preferred_access_method = :m, access_confidence = :c,
       confirmed_success_count = confirmed_success_count + 1, status = 'ACTIVE', updated_at = now()
 WHERE id = :pid
   AND status IN ('DISCOVERY_REQUIRED','LEARNING','DEGRADED');
```
so two workers flushing the same profile concurrently cannot double-promote or corrupt the count: the
first winner's own `UPDATE` moves `status` to `ACTIVE`, which is outside the promotable set, so the
`status IN (...)` predicate alone blocks every later apply for that profile — same method or a different
one. The unique `(profile_id, method_type, method_name)` on stats + single-UPDATE-per-key flush protect
the underlying counters. A non-qualifying sequence leaves the profile un-promoted (US1 "a non-qualifying
sequence does not promote").

### Same-method re-promotion from `DEGRADED` (2026-08-16, Task 3.2)

The `WHERE` clause originally also required `AND (preferred_access_method IS NULL OR
preferred_access_method <> :m)` (mirrored for `preferred_extraction_method`) — i.e. the preferred-method
column had to actually *change*. That extra predicate was redundant with the concurrency guard above (the
`status IN (...)` filter was already the sole thing preventing a double-apply, since the first promotion's
own `UPDATE` moves `status` out of the promotable set regardless of which method won) — but it also
wrongly blocked a **legitimate** case: a `DEGRADED` profile whose best method, on re-validation, turns out
to be the *same* one it already had. Since discovery/re-validation only ever runs for profiles the
resolver still serves and nothing else flips `status` back to `ACTIVE`, that profile was parked
`DEGRADED` permanently (measured live: `fqtoners.com`). The predicate is dropped in the shipped statement
above: any `decision.promote = True` (validation succeeded) on a `DEGRADED`/`LEARNING`/
`DISCOVERY_REQUIRED` profile promotes it to `ACTIVE`, whether or not the winning method changed.

This re-promotion path also **re-bases** the promoted method's `strategy_attempt_stats` row (`attempt_count
= success_count = N`, `failure_count = 0`, `success_rate = 1`, `avg_confidence` = this cycle's own batch
average — never a lifetime-blended value) when, and only when, the promoted method is the profile's
already-preferred one for that type AND its status immediately before this flush cycle was `DEGRADED`
(`app_shared/strategy/flush.py::_rebase_method_stats`, shared with the discovery-seed path's
`rebase_stats_after_discovery`, `contracts/rediscovery.md`). Without this, the *lifetime* counters that
tripped a rediscovery condition (§2 `success_rate`, §4 `avg_confidence` for `EXTRACTION` rows) would
survive the promotion unchanged and the profile would immediately re-degrade on the very next evaluation —
the same "remedy didn't clear the state that triggered it" bug class documented in
`contracts/rediscovery.md`'s 2026-08-15 runaway fix. A promotion driven by a genuinely *different* winning
method is unaffected by this re-base (no stale-`DEGRADED` baggage on that method's row to clear).
