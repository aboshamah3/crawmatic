# Contract: Products API (`/v1/products`)

Router `apps/api/app/routers/products.py`. Every endpoint runs on the SPEC-03 auth-seam session (`deps.get_current_principal` → `set_workspace_context` already applied) and is scope-gated via `deps.require_scopes(...)`. All reads/writes use `app_shared.repository.scoped_select`/`scoped_get`; RLS backs them (FR-016, FR-002).

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| `POST` | `/v1/products` | `products:write` | Create a product (+ default variant if none given) |
| `GET` | `/v1/products` | `products:read` | List (cursor-paginated) |
| `GET` | `/v1/products/{id}` | `products:read` | Get one (workspace-scoped) |
| `PATCH` | `/v1/products/{id}` | `products:write` | Partial update |
| `DELETE` | `/v1/products/{id}` | `products:write` | Delete (hard now / archive when history exists) |
| `POST` | `/v1/products/bulk-upsert` | `products:write` | Set-based bulk upsert (see `catalog-bulk-upsert.md`) |

## POST /v1/products
Request (`ProductCreate`): `external_id?`, `sku?`, `title` (req), `brand?`, `barcode?`, `url?`, `status?` (default `active`), optional `price?`+`currency?` (used only to seed the default variant), optional `variants: [VariantCreate]`.
Behavior: insert product; if `variants` empty/absent → derive **one** default variant (`default-variant.md`), requiring `price`+`currency` on the payload; else insert the given variants (no extra default). One transaction. → `201` `ProductResponse` (incl. its variants).
Errors: missing `title` → `422`; simple product with no `price`/`currency` → `422`; malformed money/currency → `422` (VII); missing scope → `403`.

## GET /v1/products
Query: `limit?` (default 50, cap 500), `cursor?` (opaque). → `200 {items: [ProductResponse], next_cursor: str|null}` (`pagination.md`). Keyset order `(created_at, id)`.

## GET /v1/products/{id}
`scoped_get(Product, id, ws)` → `200 ProductResponse` or `404` (another workspace's id is indistinguishable from nonexistent — no cross-workspace leak, SC-004).

## PATCH /v1/products/{id}
Partial update of mutable fields; `scoped_get` first → `404` if absent. → `200 ProductResponse`.

## DELETE /v1/products/{id}
No dependent history exists in this spec → **hard delete**; structured to archive-by-status (`status=archived`) once history exists. → `200 {id, outcome: "hard_deleted" | "archived"}` (FR-017). `404` if absent/other-workspace.
