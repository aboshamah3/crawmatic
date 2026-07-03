# Contract: Product Groups API (`/v1/product-groups`)

Router `apps/api/app/routers/product_groups.py`. Auth-seam session + `require_scopes`. **Scope mapping (documented):** no dedicated group scope exists in the §33 vocabulary, so group management reuses the resource write scopes — group create/update/delete and add/remove-item require `products:write` (and, when the item is a variant, `variants:write`); reads require `products:read`. This mapping is called out in the plan/spec Assumptions.

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| `POST` | `/v1/product-groups` | `products:write` | Create a group |
| `GET` | `/v1/product-groups` | `products:read` | List (cursor-paginated) |
| `GET` | `/v1/product-groups/{id}` | `products:read` | Get one (with items) |
| `PATCH` | `/v1/product-groups/{id}` | `products:write` | Update name/description/status |
| `DELETE` | `/v1/product-groups/{id}` | `products:write` | Delete (hard now / archive later) |
| `POST` | `/v1/product-groups/{id}/items` | `products:write` (+`variants:write` if variant) | Add a product or variant item |
| `DELETE` | `/v1/product-groups/{id}/items/{item_id}` | `products:write` | Remove an item |

## POST /v1/product-groups
`GroupCreate`: `name` (req), `description?`, `status?`. `unique(workspace_id, name)` → duplicate `409`. → `201 GroupResponse`.

## POST /v1/product-groups/{id}/items
`GroupItemCreate`: exactly one of `product_id` | `product_variant_id`. Referenced entity MUST resolve in the same workspace (composite FK + app pre-check, `workspace-consistency.md`) → cross-workspace/nonexistent `422`/`404`. Duplicate membership (partial unique) → `409` (FR-013). → `201 GroupItemResponse`.

## DELETE /v1/product-groups/{id}/items/{item_id}
Workspace-scoped delete → `204` (idempotent; absent → still `204` or `404`, chosen `404` for a specific missing id under an existing group).

## DELETE /v1/product-groups/{id}
Hard delete now (no dependent history); structured for archive-by-status → `200 {id, outcome}` (FR-017).
