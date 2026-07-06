# Contract — Variant selection (US3)

`scrape_core/browser/variant.py`

## API

```python
class VariantConfigError(ValueError):
    """Malformed/unsupported variant_selector_config → SELECTOR_BROKEN. Carries error_code."""

def resolve_variant_values(config: dict, match) -> dict[str, str]:
    """Off-reactor (in load_targets): resolve every action's value_from against the match row.
    Returns {action_index: resolved_value}. Raises VariantConfigError if a value_from is unknown
    or resolves to None/missing."""

def parse_variant_config(config: dict | None, resolved_values: dict) -> list[PageMethod]:
    """Pure translation → ordered scrapy_playwright PageMethod list. None → []. Raises
    VariantConfigError on unknown type / missing required key / forbidden action."""
```

## Rules (see data-model.md §2 for the JSON shape)

- `config is None` → `[]` (no interaction; FR-004, US3 AS2).
- `version` must be `1`; else `VariantConfigError`.
- `actions` executed in order; allowlist ONLY: `click`, `select_option`, `fill`, `wait_for_selector`,
  `wait_for_timeout`, `wait_for_load_state`. Any other `type` (esp. `evaluate`) → `VariantConfigError`
  (never executed — R2 security).
- Element actions require `selector`; `select_option`/`fill` require `value` **or** `value_from`.
- `value_from` allowlist: `options.<key>` → `match.competitor_variant_options[<key>]`; `identifier` →
  `match.competitor_variant_identifier`; `sku` → `match.competitor_variant_sku`. Unresolved → error.
- Every `wait_*` method carries the effective browser timeout (R10) as `timeout`.
- Optional trailing `settle` → a `wait_for_selector` and/or `wait_for_load_state` PageMethod appended
  after the actions (before extraction).

## Failure → error code (R3)

| Condition | Code |
|---|---|
| Unknown/forbidden action type, missing required key, unresolved `value_from`, bad `version` | `SELECTOR_BROKEN` |
| Configured element not found / not interactable within timeout at run time (Playwright raises) | `VARIANT_NOT_FOUND` |

No partially-interacted page state is ever persisted as a valid price (US3 AS3): a raised
`VariantConfigError` (parse time) or a Playwright interaction error (run time) both terminate the
attempt as a failed `ScrapeResult` before extraction.

## Acceptance mapping

- US3 AS1 — config present, price updates after selection ⇒ actions run, settle waits, post-selection
  price extracted.
- US3 AS2 — no config ⇒ `parse_variant_config` returns `[]`, default price extracted, no interaction.
- US3 AS3 — target missing/uninteractable ⇒ `VARIANT_NOT_FOUND`/`SELECTOR_BROKEN`, clean fail.
