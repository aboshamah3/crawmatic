# Contract: Variants API (`/v1/variants`)

Router `apps/api/app/routers/variants.py`. Auth-seam session + `require_scopes`. Variants are created via their parent product (`POST /v1/products`) or bulk-upsert; this router exposes read/update + bulk-upsert (no standalone create, no delete that could orphan the ≥1-variant invariant).

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| `GET` | `/v1/variants` | `variants:read` | List (cursor-paginated); optional `product_id` filter (workspace-scoped) |
| `GET` | `/v1/variants/{id}` | `variants:read` | Get one |
| `PATCH` | `/v1/variants/{id}` | `variants:write` | Partial update (incl. price/currency) |
| `POST` | `/v1/variants/bulk-upsert` | `variants:write` | Set-based bulk upsert (see `catalog-bulk-upsert.md`) |

## GET /v1/variants
Query: `product_id?` (composite-scoped to workspace), `limit?`, `cursor?`. → `200 {items:[VariantResponse], next_cursor}`.

## GET /v1/variants/{id}
`scoped_get(ProductVariant, id, ws)` → `200 VariantResponse` or `404`.

## PATCH /v1/variants/{id}
Update mutable fields (`title`, `sku`, `barcode`, `url`, `option_values`, `current_price`, `currency`, `status`). Money/currency validated at boundary (VII) → malformed `422`. `title` change must keep `unique(workspace_id, product_id, title)` → conflict `409`. → `200 VariantResponse`.

## Last-variant invariant (FR-006)
A variant operation that would leave its product with **zero** variants (e.g. archiving/removing the sole variant) is prevented (`409`/`422`) or re-establishes a default. Product always retains ≥1 variant.
