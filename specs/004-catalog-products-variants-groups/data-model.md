# Phase 1 Data Model: Catalog — Products, Variants & Groups

Source of truth: `PROJECT_SPEC.md` §22 (table shapes + unique constraints), §32 (RLS, workspace-local FKs), §19 (money/currency), §21 (UUIDv7/TIMESTAMPTZ). Built on SPEC-02 (`Base`, `WorkspaceScopedBase`, `TimestampMixin`, `TZDateTime`, `Money`, `enum_column`, `emit_rls_policy`, `NAMING_CONVENTION`) and SPEC-03 (`WORKSPACE_OWNED_MODELS`, scoped helpers, `set_workspace_context`).

All four tables are **workspace-owned** → `WorkspaceScopedBase` (`workspace_id NOT NULL`, indexed), added to `WORKSPACE_OWNED_MODELS`, and get `emit_rls_policy(...)` in the creating migration.

---

## Enums (`app_shared/enums.py`, extend)

| Enum | Values | Column(s) |
|------|--------|-----------|
| `ProductStatus` | `active`, `archived` | `products.status` |
| `VariantStatus` | `active`, `archived` | `product_variants.status` |
| `GroupStatus` | `active`, `archived` | `product_groups.status` |

All `StrEnum`, string-backed via `enum_column(...)` → rendered as `VARCHAR(32)` (never a PG-native enum). `product_group_items` has **no** status column (§22) — no enum. `archived` is the terminal state for FR-017 archive-by-status deletion.

---

## Entity: Product (`products`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | from `Base` |
| `workspace_id` | `Uuid` | no | from `WorkspaceScopedBase`, indexed; FK → `workspaces.id` |
| `external_id` | `Text` | yes | store-side id (Woo/Salla) |
| `sku` | `Text` | yes | |
| `title` | `Text` | no | required |
| `brand` | `Text` | yes | |
| `barcode` | `Text` | yes | |
| `url` | `Text` | yes | |
| `status` | `VARCHAR(32)` (`ProductStatus`) | no | `active` default |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | from `TimestampMixin` |

**Constraints / indexes**
- `unique(workspace_id, id)` — enables composite-FK targeting by children (D3).
- `Index` **partial unique** `(workspace_id, external_id) WHERE external_id IS NOT NULL`.
- `Index` **partial unique** `(workspace_id, sku) WHERE sku IS NOT NULL`.
- FK `workspace_id → workspaces.id`.

**Invariant.** Always has ≥1 `ProductVariant` (FR-005/FR-006). No price column (Principle III — pricing is variant-level only).

---

## Entity: ProductVariant (`product_variants`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | |
| `workspace_id` | `Uuid` | no | indexed; FK → `workspaces.id` |
| `product_id` | `Uuid` | no | parent; composite FK (see below) |
| `external_id` | `Text` | yes | |
| `sku` | `Text` | yes | |
| `barcode` | `Text` | yes | |
| `title` | `Text` | no | required; default variant inherits product title (fallback `"Default"`) |
| `option_values` | `JSONB` | yes | e.g. `{"color":"red","size":"M"}` |
| `current_price` | `Money` → `NUMERIC(18,4)` | no | finite decimal, rejects float/NaN/Inf/over-scale (VII) |
| `currency` | `CHAR(3)` | no | 3-letter code, validated at boundary |
| `url` | `Text` | yes | |
| `status` | `VARCHAR(32)` (`VariantStatus`) | no | |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | `TimestampMixin` |

**Constraints / indexes**
- `unique(workspace_id, id)` — composite-FK target for group items.
- Partial unique `(workspace_id, external_id) WHERE external_id IS NOT NULL`.
- Partial unique `(workspace_id, sku) WHERE sku IS NOT NULL`.
- `unique(workspace_id, product_id, title)` — full (non-partial); guarantees one row per (product, title), so one default per product.
- **Composite FK** `(workspace_id, product_id) → products(workspace_id, id)` — workspace-local by construction (D3).
- FK `workspace_id → workspaces.id`.

`current_price`/`currency` NOT NULL per §22 (`numeric(18,4)` / `char(3)` are non-nullable in the shape). The default-variant derivation must therefore supply price+currency (inherited from the product-create payload's optional price/currency, which the create schema requires when no explicit variants are given).

---

## Entity: ProductGroup (`product_groups`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | |
| `workspace_id` | `Uuid` | no | indexed; FK → `workspaces.id` |
| `name` | `Text` | no | |
| `description` | `Text` | yes | |
| `status` | `VARCHAR(32)` (`GroupStatus`) | no | |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | `TimestampMixin` |

**Constraints / indexes**
- `unique(workspace_id, id)` — composite-FK target for group items.
- `unique(workspace_id, name)` — one group name per workspace.
- FK `workspace_id → workspaces.id`.

---

## Entity: ProductGroupItem (`product_group_items`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | |
| `workspace_id` | `Uuid` | no | indexed; FK → `workspaces.id` |
| `product_group_id` | `Uuid` | no | composite FK → `product_groups(workspace_id, id)` |
| `product_id` | `Uuid` | yes | composite FK → `products(workspace_id, id)` (MATCH SIMPLE: unchecked when NULL) |
| `product_variant_id` | `Uuid` | yes | composite FK → `product_variants(workspace_id, id)` |
| `created_at` | `TIMESTAMPTZ` | no | **`TZDateTime` directly** — no `updated_at` (§22 shape, like `RefreshToken`) |

**Constraints / indexes**
- Partial unique `(workspace_id, product_group_id, product_id) WHERE product_id IS NOT NULL`.
- Partial unique `(workspace_id, product_group_id, product_variant_id) WHERE product_variant_id IS NOT NULL`.
- Composite FKs (all workspace-local): `(workspace_id, product_group_id) → product_groups(workspace_id, id)`; `(workspace_id, product_id) → products(workspace_id, id)`; `(workspace_id, product_variant_id) → product_variants(workspace_id, id)`.
- FK `workspace_id → workspaces.id`.

An item references **either** a product **or** a variant. (App-layer rule: exactly one of the two is non-null — enforced in the schema/router; DB allows either via MATCH SIMPLE nullable composite FKs. Duplicate membership rejected by the partial uniques.)

---

## Isolation & RLS summary (§32, Principle II)

| Table | Workspace-owned | RLS | In `WORKSPACE_OWNED_MODELS` |
|-------|-----------------|-----|-----------------------------|
| `products` | yes | **yes** (`emit_rls_policy` in creating migration) | yes |
| `product_variants` | yes | **yes** | yes |
| `product_groups` | yes | **yes** | yes |
| `product_group_items` | yes | **yes** | yes |

RLS policy per table (from `emit_rls_policy`): `ENABLE` + `FORCE` ROW LEVEL SECURITY + `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` — fail-closed to **zero rows** when no context is set. The request session has `set_workspace_context` applied by the SPEC-03 auth seam before any catalog query. All reads/writes go through `scoped_select`/`scoped_get` (app-layer filter) — the two-layer model.

---

## Identity resolution (bulk upsert, FR-011, §24)

Order per record: `external_id` → `sku` → (variants only) `(product_id, title)`. First present key wins; the batch is partitioned by identity kind and each partition upserts via `ON CONFLICT` inferring the matching (partial) unique index (see research D2/D4). In-batch duplicates on a resolved identity collapse **last-wins** (FR-012) before statement build.

---

## Invariants & state

- **Default variant**: creating/upserting a product with no explicit variants yields exactly one default variant (title = product title, fallback `"Default"`; inherits sku/url; price/currency from payload). Explicit variants → no extra default. (FR-005, SC-001)
- **≥1 variant always**: an update that would remove a product's last variant is prevented or re-establishes a default. (FR-006)
- **Deletion (FR-017)**: hard-delete while no dependent history exists (true in this spec); model + endpoint structured for archive-by-status (`status = archived`); response indicates which outcome occurred.
- **Money/currency (VII)**: `current_price` finite `NUMERIC(18,4)`; `currency` 3-letter; rejected at boundary otherwise; no cross-currency comparison.
- **Uniqueness (SC-008)**: partial uniques on external_id/sku (where present), full unique variant `(workspace_id, product_id, title)`, group `(workspace_id, name)`, group-item memberships.
