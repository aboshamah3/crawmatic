# Contract: Pure alert engine — `app_shared/alerts/engine.py`

**DB/framework-free.** Imports **stdlib `decimal` only** (plus `app_shared.enums` for the
alert enums and, optionally, `app_shared.money.parse_money` for boundary rejection).
**MUST NOT** import sqlalchemy, celery, fastapi, scrapy, redis. Exhaustively unit-testable.

## Constants

```python
QUANT = Decimal("0.0001")                 # 4dp
SEVERITY_BY_TYPE: dict[AlertType, AlertSeverity] = {
    AlertType.NO_COMPETITOR_DATA: AlertSeverity.LOW,
    AlertType.RISK: AlertSeverity.CRITICAL,
    AlertType.HIGH_PRICE: AlertSeverity.HIGH,
    AlertType.CHANCE_TO_INCREASE_PRICE: AlertSeverity.MEDIUM,
    AlertType.NORMAL: AlertSeverity.NONE,
    AlertType.CLOSE_TO_COMPETITORS: AlertSeverity.MEDIUM,
}
```

## Input value objects (lightweight, framework-free)

```python
@dataclass(frozen=True)
class CompetitorPrice:
    match_id: uuid.UUID
    price: Decimal | None
    currency: str | None
    success: bool
    comparable: bool

@dataclass(frozen=True)
class ComparableSplit:
    included_prices: list[Decimal]          # sorted or raw; benchmarks derived from these
    mismatched_match_ids: list[uuid.UUID]   # currency present and != client → CURRENCY_MISMATCH

@dataclass(frozen=True)
class AlertOutcome:
    type: AlertType
    severity: AlertSeverity
    cheapest: Decimal | None
    average: Decimal | None
    highest: Decimal | None
    comparable_count: int
    benchmark_price: Decimal | None
    discount_vs_average: Decimal | None
    mismatched_match_ids: list[uuid.UUID]
    message: str
    details: dict
```

## Functions

### `filter_comparable(client_currency, rows) -> ComparableSplit`
- Included iff `row.success and row.comparable and row.price is not None and row.currency
  == client_currency`.
- `mismatched_match_ids` = rows where `row.currency is not None and row.currency !=
  client_currency` (currency-mismatch set — the task flips their `comparable=false` +
  `CURRENCY_MISMATCH`). Rows failing for other reasons (not success, price None) are simply
  excluded, **not** flagged mismatch.

### `discount_vs_average(average, client_price) -> Decimal`
- `((average - client_price) / average) * 100`, then `.quantize(QUANT, ROUND_HALF_UP)`.
- Precondition `average > 0` (guaranteed: only called when comparables exist and average of
  positive prices). Never called when `comparable_count == 0`.

### `decide(client_price, cheapest, average, highest, comparable_count) -> (AlertType,
Decimal | None)` — the ordered §23 tree
```text
0. client_price is None (defensive)              -> NO_COMPETITOR_DATA, discount=None
1. comparable_count == 0                         -> NO_COMPETITOR_DATA, discount=None
2. client_price > highest                        -> RISK
3. client_price > cheapest                       -> HIGH_PRICE
4. d = discount_vs_average(average, client_price) # Decimal, quantized 4dp
5. d > Decimal("5")                              -> CHANCE_TO_INCREASE_PRICE
6. Decimal("1") <= d <= Decimal("5")             -> NORMAL
7. Decimal("0") <= d < Decimal("1")              -> CLOSE_TO_COMPETITORS
8. else (unreachable defensive)                  -> HIGH_PRICE
```
All price/threshold compares are `Decimal` vs `Decimal` (never float). Steps 2–3 use `>`
strictly (equal-to-highest/cheapest falls through to the discount math). Step 0 is
defensive: `product_variants.current_price` is NOT NULL (SPEC-04), so `analyze` never
passes a null client price in practice; the guard exists so the pure function degrades to
NO_COMPETITOR_DATA rather than raising if ever called with `None`.

Note on `transition(...)`: because `severity_for()` makes severity a pure function of type,
a same-type-with-different-severity input cannot arise from the real engine; the transition
rule's "same-type severity change → UPDATED" case is a defensive branch, exercised in tests
only via a hand-constructed input (I1).

### `severity_for(alert_type) -> AlertSeverity`
- `return SEVERITY_BY_TYPE[alert_type]` — the only source of severity (FR-011).

### `analyze(client_price, client_currency, competitor_rows) -> AlertOutcome`
- `split = filter_comparable(...)`; `count = len(split.included_prices)`.
- benchmarks: `min/mean/max` of `included_prices` (Decimal mean = `sum/ count`, **not**
  quantized here — only `discount_vs_average` is quantized; benchmarks stored as-is,
  NUMERIC(18,4)); `None`×3 when `count == 0`.
- `type, discount = decide(...)`; `severity = severity_for(type)`.
- `benchmark_price`: `highest` for RISK, `cheapest` for HIGH_PRICE, `average` for the
  discount-based types, `None` for NO_COMPETITOR_DATA. (Exact mapping documented; carried
  into `variant_alert_states.benchmark_price`.)
- `message`/`details`: deterministic strings/dicts (include count, discount, mismatched
  ids) — pure, no timestamps inside (timestamps are added by the task so the engine stays
  time-independent and its output is byte-identical across runs, SC-001).

### `transition(prev_type, prev_severity, new_type, new_severity, *, had_history) ->
AlertEventType | None`
The ordered rule (research D5 / FR-013 / Clarifications):
```text
prev is None and new in {NORMAL}                 -> None            # created NORMAL, no event
prev is None and new not NORMAL                  -> CREATED
(prev_type, prev_severity) == (new_type, new_sev)-> None            # UNCHANGED, not persisted
prev not NORMAL and new in {NORMAL}              -> RESOLVED
prev in {NORMAL} and new not NORMAL and had_history -> REOPENED
prev not NORMAL and new not NORMAL (differ)      -> UPDATED
```
"NORMAL" here means the NORMAL/NONE resolved state. `had_history` = a prior
`variant_alert_states` row exists (distinguishes CREATED from REOPENED). Returns `None` for
the no-event and UNCHANGED cases (the task then writes no event, only advances
`last_seen_at`).

## Determinism guarantees (unit tests)

- Every §23 branch (1–8) hit by a fixture (step 8 via a constructed degenerate input).
- Boundary table (research D2): 0.0000 / 0<..<1 / 1.0000 / 1<..<5 / 5.0000 / >5 map exactly.
- Half-up rounding cases (e.g. a value whose 5th decimal is 5 rounds up before compare).
- NaN/Infinity/over-scale client or competitor price rejected at the boundary (raises, not
  silently coerced).
- Severity map is total and exact (parametrized over all six types).
- `transition` truth table: all six cases + the two `None` cases.
- Re-running `analyze` with identical inputs yields an equal `AlertOutcome` (no time/random).

## Acceptance

`app_shared/alerts/engine.py` imports nothing outside stdlib `decimal` +
`app_shared.enums` (+ optional `app_shared.money`); a `grep`/AST test asserts the ban
(mirrors the SPEC-08 import-boundary test). All decision/severity/transition tests pass with
zero infra.
