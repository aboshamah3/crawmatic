# Contract: Money type

Module: `app_shared/money.py` → `Money(TypeDecorator)`.

## Definition

- `impl = Numeric(precision=18, scale=4, asdecimal=True)` → Postgres `NUMERIC(18,4)`.
- `cache_ok = True`.

## Bind-time validation (`process_bind_param`) — guarantees

| Input | Result |
|---|---|
| `None` | passes (nullable columns) |
| `float` (e.g. `1.1`) | **rejected** — never float (§19) |
| `Decimal("NaN")`, `Decimal("Infinity")`, `Decimal("-Infinity")` | **rejected** (`ValueError`) |
| `Decimal("1.23456")` (>4 dp) | **rejected** (`ValueError`) — no silent rounding |
| `Decimal("19.99")`, `Decimal("0.0001")`, `int` | accepted → stored as `Decimal` |

## Result-time guarantee (`process_result_value`)

- Returns a `Decimal`. Valid in-scale values round-trip **exactly**; never surfaced as a float (SC-004).

## Scope

- Value type only. Currency-code storage and cross-currency comparison are out of scope (§19; `comparable=false` / `CURRENCY_MISMATCH` handled in later specs).

## Consumers

- Any money column (e.g. `product_variants.current_price` in SPEC-04). Demo table uses one `Money` column.

## Tests

- `tests/unit/test_money.py` — each rejection case raises; in-scale `Decimal` round-trips exactly as `Decimal`. Fully DB-independent.
