# Contract: Default-variant derivation (`app_shared/catalog/default_variant.py`)

Pure, framework-agnostic. Used by `POST /v1/products` and bulk upsert. Satisfies FR-005/006, SC-001, Principle III.

## Functions
- `derive_default_variant(product: Mapping) -> dict` — returns the field dict for the single default variant of a product supplied without explicit variants:
  - `title` = `product["title"]` if present and non-empty, else `"Default"`.
  - `sku` = `product.get("sku")`; `url` = `product.get("url")` (inherited when provided, else null).
  - `current_price` = `product["price"]`; `currency` = `product["currency"]` (a "simple product" create MUST supply these — variant price is NOT NULL per §22).
  - `option_values` = `None`; `status` = `active`.
  - `product_id` is filled by the caller after the product row exists.
- `ensure_at_least_one(product: Mapping, variants: list) -> list` — returns `variants` unchanged if non-empty, else `[derive_default_variant(product)]`.

## Rules
- A product **with** explicit variants gets **no** extra default (FR-005).
- A product **without** variants gets exactly **one** default (SC-001) — never zero, never two.
- The one-default-per-product shape is compatible with `unique(workspace_id, product_id, title)` (a product's default title equals its product title / `"Default"`).

## Unit tests (no DB)
- Title inheritance + `"Default"` fallback.
- sku/url/price/currency inheritance.
- `ensure_at_least_one` passthrough for non-empty; single-default for empty.
- No default added when explicit variants present.
