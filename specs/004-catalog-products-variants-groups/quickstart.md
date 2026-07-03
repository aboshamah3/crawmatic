# Quickstart / Validation: Catalog — Products, Variants & Groups

How to validate SPEC-04. DB-independent logic is fully unit-tested **in this environment**; items needing a live Postgres are authored and **skipped** until run on a PG-capable host (no Docker daemon / live Postgres here). Details live in `contracts/` and `data-model.md` — this is the run guide.

## Prerequisites
- uv workspace synced: `uv sync`.
- No new third-party dependencies (uses existing SQLAlchemy pg dialect, FastAPI/Pydantic, stdlib).
- Live-DB validation additionally needs a reachable Postgres 17 (`MIGRATION_DATABASE_URL`) with the `crawmatic_app` (no BYPASSRLS) + `crawmatic_auth` (BYPASSRLS) roles from SPEC-03, plus the SPEC-03 head already applied.

## 1. Unit tests (run here — no DB)
```bash
uv run pytest tests/unit -q
```
Expected new/extended coverage (all pass without a DB):
- `test_catalog_models.py` — §22 shapes, partial-index `postgresql_where` render, composite FKs, `unique(workspace_id,id)` parents, enums, no `updated_at` on group items, Money `NUMERIC(18,4)`, `VARCHAR(32)` status.
- `test_rls_catalog.py` — `emit_rls_policy` renders ENABLE+FORCE+fail-closed policy for all four tables.
- `test_default_variant.py` — derivation (title inherit / `"Default"` fallback, sku/url/price/currency inherit) + `ensure_at_least_one`.
- `test_catalog_upsert.py` — `build_*_upsert` compiled SQL has correct `ON CONFLICT (...) [WHERE ...] DO UPDATE SET ...`; identity precedence; last-wins dedup; **bounded** statement count (≤3/table, no per-row loop).
- `test_pagination.py` — cursor round-trip, malformed → `InvalidCursor`, `clamp_limit` (None→50, 9999→500, 0→1).
- `test_workspace_consistency.py` — in-workspace accepted; cross-workspace/nonexistent rejected; `exactly_one_of`.
- `test_catalog_scope_gating.py` — routers declare correct `require_scopes` (read vs write per family; group items require variant write when variant).
- `test_catalog_scoping_guard.py` — CI guard flags a planted `select(Product)` without `workspace_id`; clean tree passes.
- `test_migration_offline_catalog.py` — `alembic upgrade head --sql` renders 4 tables + 6 partial indexes + 12 RLS statements.
- `test_import_boundaries.py` (extended) — `app_shared.models.catalog` / `app_shared.pagination` / `app_shared.catalog.*` import **no** fastapi/scrapy/twisted/playwright.

## 2. CI guards (run here)
```bash
uv run python scripts/check_workspace_scoping.py   # OK — 4 catalog models now guarded, no unscoped access
bash scripts/check_single_head.sh                  # single head after the catalog revision
```

## 3. Migration offline render (run here)
```bash
SPECIFY_FEATURE_DIRECTORY=specs/004-catalog-products-variants-groups uv run alembic upgrade head --sql | less
# Expect: CREATE TABLE products|product_variants|product_groups|product_group_items,
#         CREATE UNIQUE INDEX ... WHERE <col> IS NOT NULL (x6),
#         ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY + CREATE POLICY (x4 tables).
```

## 4. Live-DB validation (run on a Postgres host — skipped here)
```bash
uv run alembic upgrade head        # creates the 4 tables + RLS online
uv run pytest tests/integration -q # live-marked scenarios
```
Scenarios (map to spec Acceptance / Success Criteria):
- **US1 / SC-001** — `POST /v1/products` with no variants → exactly one default variant carrying product price/currency/sku/url; with explicit variants → those exact variants, no spurious default. Read/update/list/delete round-trip within the workspace.
- **US2 / SC-002/003** — bulk-upsert a batch → all created (each product ≥1 variant); re-push with changes → matched rows updated in place (external_id → sku → product+title), **0** duplicates; in-batch same-identity rows resolve last-wins; a large batch runs in a bounded number of statements (assert via query log — no per-row loop).
- **US3 / SC-008** — create group, add a product + a variant item, list, remove; duplicate membership rejected; cross-workspace item reference rejected.
- **US4 / SC-004/005/006** — workspace-A caller sees **0** of workspace-B's products/variants/groups (by id and in lists); a query with the app filter omitted still returns 0 rows (RLS); no workspace context → 0 rows (fail-closed); a `products:read`-only key cannot write; a `products:write` key can; the scoping guard fails CI on any introduced unscoped catalog query.
- **VII / SC-007** — non-finite/over-precise price or malformed currency rejected at the boundary (`422`), never stored.
- **FR-017** — delete returns `{outcome: "hard_deleted"}` now; structured for `"archived"` once history exists.

## Rollback
```bash
uv run alembic downgrade -1   # drops the 4 catalog tables (FK-safe order); returns to head 55da7d6d939d
```
