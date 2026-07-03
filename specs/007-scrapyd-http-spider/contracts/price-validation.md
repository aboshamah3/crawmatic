# Contract: price validation & confidence gate (`scrape_core.validation`)

Pure. `validate_candidate(candidate, validation_rules, confidence_cfg) -> Accepted(price, comparable) | Rejected(error_code, message)` (FR-008/009/010/011, §17/18/19, US3). A wrong price is worse than a missing one.

## Order of checks (first failure wins the error code)

1. **Money boundary** — `app_shared.money.parse_money(candidate.raw_price_text)` → exact `Decimal`. Rejects `float`/`NaN`/`Infinity`/over-scale (> 4 dp — **not** rounded) → `INVALID_PRICE_FORMAT`.
2. **Positivity** — `price > 0` else `INVALID_PRICE_FORMAT`.
3. **Currency** — if a required/expected currency is configured (`validation_rules.required_currency` or the client variant currency) and differs → **not** rejected: mark `comparable=false`, record `CURRENCY_MISMATCH` (warning), keep the price (excluded from comparison; no FX — FR-011).
4. **Bounds** — `validation_rules.min_price` / `max_price` (parsed via the same money boundary) → out of range rejects.
5. **Text rejects** — `validation_rules.reject_if_text_contains` matched against `candidate.matched_text` (old / installment / discount / "save X" / shipping) → reject.
6. **Confidence gate** — `candidate.confidence >= confidence_cfg["min_accepted_confidence"]` (default **0.75** via `resolve_confidence_rules`) else `LOW_CONFIDENCE_PRICE`. A `SINGLE_NUMBER` candidate (0.40) fails this by default (edge case: only an unlabeled number on the page → rejected).

## Outcome mapping

- **Accepted** → `success=true` observation with the `Decimal` price + `comparable` flag; `match_current_prices` upserted.
- **Rejected** → `success=false` observation with the error code; `match_current_prices` price fields **not** overwritten (FR-014).
- **Currency mismatch** is an accepted-but-`comparable=false` observation (saved, excluded from comparison).

## Reuse (Principle VII — one money/confidence source)

- `app_shared.money.parse_money` is the single §19 boundary (no re-implementation).
- `app_shared.profiles.confidence.resolve_confidence_rules` supplies the min-accepted + per-method defaults (DB-tunable).
- `validation_rules` semantics mirror `app_shared.profiles.validation` (same keys: `required_currency`, `min_price`, `max_price`, `reject_if_text_contains`, `prefer_text_contains`).

## Tests (unit)

Decimal exactness / float+NaN+Infinity+over-scale rejection (never rounds); `> 0`; currency-match vs `CURRENCY_MISMATCH` (`comparable=false`); min/max; each `reject_if_text_contains` term; confidence below 0.75 → `LOW_CONFIDENCE_PRICE`; single-number 0.40 rejected.
