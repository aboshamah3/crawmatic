# Contract: Set-based Bulk Upsert (`app_shared/catalog/upsert.py`)

Pure, framework-agnostic core (SQLAlchemy pg dialect only). Builds statements; does **not** execute. Unit-tested by compiling to the `postgresql` dialect SQL string. Satisfies FR-010/011/012, SC-002/003.

## Functions
- `resolve_identity(row, *, is_variant) -> ("external_id"|"sku"|"product_title", value)` — first present of `external_id` → `sku` → (variants) `(product_id, title)`. (FR-011 order.)
- `dedup_last_wins(rows, key_fn) -> list` — collapse rows sharing a resolved identity, keeping the **last** occurrence, stable order. (FR-012.)
- `build_products_upsert(rows_partition, identity_kind) -> Insert` — one `pg_insert(Product).values([...]).on_conflict_do_update(index_elements=[...], index_where=<partial predicate or None>, set_={col: excluded[col] for updatable cols})`.
- `build_variants_upsert(rows_partition, identity_kind) -> Insert` — same for `ProductVariant` (identity kinds: external_id, sku, `(product_id,title)`).
- `plan_upsert(rows, *, is_variant) -> list[Insert]` — partition by identity kind, dedup each, build one statement per non-empty partition. **Bounded** count (≤3 per table) regardless of `len(rows)` — no per-row loop.

## ON CONFLICT inference (the partial-index nuance, research D2)
Targeting a **partial** unique index requires the inference `index_where` to match the index predicate exactly:
```python
pg_insert(Product).on_conflict_do_update(
    index_elements=["workspace_id", "external_id"],
    index_where=text("external_id IS NOT NULL"),
    set_={...},
)
```
→ compiles to `... ON CONFLICT (workspace_id, external_id) WHERE external_id IS NOT NULL DO UPDATE SET ...`. The `(workspace_id, product_id, title)` variant identity targets the **full** unique (no `index_where`). A statement infers exactly **one** arbiter, hence partitioning by identity kind.

## Variant→product resolution
Incoming variant rows name their parent by parent identity (external_id/sku) or explicit `product_id`. Resolve all parents in **one** scoped `select(Product.id).where(Product.workspace_id==ws, tuple_(external_id, sku) IN (...))` after the products upsert; map to ids; unresolved → reject (workspace-consistency). Then build the variant upsert set-based.

## Default-variant in bulk (FR-012 tail)
After products upsert, any batch product that arrived with **zero** variants gets one `derive_default_variant(...)` appended to the variant upsert set → every upserted product ends with ≥1 variant.

## Unit tests (no DB)
- `resolve_identity` precedence external_id > sku > (product_id,title).
- `dedup_last_wins` keeps the last of colliding-identity rows.
- `build_*_upsert` compiled SQL contains the correct `ON CONFLICT (...) [WHERE ...] DO UPDATE SET ...`; `excluded.*` on updatable columns only (never `id`, `created_at`, `workspace_id`).
- `plan_upsert` emits a **bounded** number of statements (≤3/table) for a large batch — no statement-per-row.
