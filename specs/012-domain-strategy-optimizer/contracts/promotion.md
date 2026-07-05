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
   AND status IN ('DISCOVERY_REQUIRED','LEARNING','DEGRADED')
   AND (preferred_access_method IS NULL OR preferred_access_method <> :m);
```
so two workers flushing the same profile concurrently cannot double-promote or corrupt the count; the
unique `(profile_id, method_type, method_name)` on stats + single-UPDATE-per-key flush protect the
underlying counters. A non-qualifying sequence leaves the profile un-promoted (US1 "a non-qualifying
sequence does not promote").
