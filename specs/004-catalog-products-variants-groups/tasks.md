---
description: "Dependency-ordered task list for SPEC-04 Catalog — Products, Variants & Groups"
---

# Tasks: Catalog — Products, Variants & Groups

**Input**: Design documents from `/specs/004-catalog-products-variants-groups/`

**Prerequisites**: plan.md, spec.md, research.md (D1–D10), data-model.md, contracts/ (9 files), quickstart.md

**Tests**: Unit tests are DB-independent and run **here**; live-Postgres acceptance tests are **authored + DEFERRED** (no Docker/live Postgres in this build env — see research D10). Deferred tasks stay unchecked `- [ ]` and are marked ⏸ DEFERRED (needs live Postgres).

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]`/`[US4]` maps a task to a spec.md user story (Setup/Foundational/Polish carry no story label)
- Every task lists an exact absolute-relative file path

---

## Scope Boundary (read first)

**IN SCOPE — exactly four catalog tables and their surface:**

- Tables: `products`, `product_variants`, `product_groups`, `product_group_items` (workspace-owned, RLS on all four in the creating migration).
- Endpoints under `/v1`: products (create/list/get/update/delete/bulk-upsert), variants (list/get/update/bulk-upsert), product-groups (create/list/get/update/delete + add/remove item).
- Behaviors: default-variant guarantee (every product ≥1 variant), set-based bulk upsert, cursor pagination, workspace isolation + scope-gating (`products:read/write`, `variants:read/write`), hard-delete-vs-archive outcome.

**OUT OF SCOPE (do NOT create anything for these — SPEC-05+):** competitors, matches, scrape-profiles, observations, prices/price-history, alerts. The catalog tables carry **no** references to those. No new API-key scopes beyond the existing `products:*`/`variants:*` (group management reuses those write scopes). No cross-currency comparison.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums and empty package scaffolding that later files import.

- [X] T001 [P] Add `ProductStatus`, `VariantStatus`, `GroupStatus` (each `StrEnum` with values `active`/`archived`, one named enum per entity per research D1) to `libs/shared/app_shared/enums.py`; no `GroupItemStatus` (group items have no status column).
- [X] T002 [P] Create `libs/shared/app_shared/catalog/__init__.py` (empty package init for the framework-agnostic catalog core).
- [X] T003 [P] Create `apps/api/app/schemas/__init__.py` (empty package init for the API-layer Pydantic DTOs).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The four ORM models, their registration, the migration + RLS, and the framework-agnostic pagination helper. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–6.

- [X] T004 Create the four ORM models in `libs/shared/app_shared/models/catalog.py` on `WorkspaceScopedBase` (per data-model.md): `Product`, `ProductVariant`, `ProductGroup` with `TimestampMixin`; `ProductGroupItem` with bare `created_at` (`TZDateTime`, no `updated_at`). Include `option_values` JSONB, `current_price` `Money`(NUMERIC(18,4)) + `currency` `CHAR(3)`; partial-unique `Index(..., unique=True, postgresql_where=text("... IS NOT NULL"))` for external_id/sku (products & variants) and the two group-item membership indexes; full `unique(workspace_id, product_id, title)` on variants and `unique(workspace_id, name)` on groups; `unique(workspace_id, id)` on `Product`/`ProductVariant`/`ProductGroup` so composite FKs resolve; workspace-local composite FKs `(workspace_id, ref_id) → parent(workspace_id, id)` (variant→product, group_item→group/product/variant, nullable ones MATCH SIMPLE); `workspace_id → workspaces.id` FK. (FR-001, FR-004, FR-007, FR-008, FR-009)
- [X] T005 Re-export `Product`, `ProductVariant`, `ProductGroup`, `ProductGroupItem` from `libs/shared/app_shared/models/__init__.py` so `Base.metadata` sees them for Alembic offline render (depends on T004).
- [X] T006 In `libs/shared/app_shared/repository.py` add the four catalog models to `WORKSPACE_OWNED_MODELS` and widen `ModelT` to `TypeVar("ModelT", bound=Base)` (research D9). Helper behavior unchanged. (FR-002)
- [X] T007 [P] Create `libs/shared/app_shared/pagination.py` (framework-agnostic, stdlib only): `encode_cursor(created_at, id)`/`decode_cursor(str)` via `base64url(json({"c":..., "id":...}))`, typed `InvalidCursor`; `clamp_limit(requested)` = `min(requested or 50, 500)`; `keyset_predicate(model, after)` building the `(created_at, id) > (c, id)` tuple seek; `{items, next_cursor}` envelope builder (fetch `limit+1`, null cursor when no more). (FR-015)
- [X] T008 Author the Alembic migration `alembic/versions/<rev>_catalog_tables.py` with `down_revision = "55da7d6d939d"`: `create_table` all four (exact §22 shapes; `String(32)` status, `Numeric(18,4)` money, `CHAR(3)` currency, `JSONB` option_values), create the partial unique indexes (`postgresql_where=sa.text("... IS NOT NULL")`), the `unique(workspace_id, id)` parents + composite FKs, then `emit_rls_policy(...)` on **all four** tables; FK-safe `downgrade()` (group_items → variants → groups/products); single head. (FR-001, FR-003, FR-008, FR-009)
- [X] T009 [P] Unit test `tests/unit/test_catalog_models.py`: table/column shapes, partial-index `postgresql_where` render, full uniques, `unique(workspace_id, id)`, composite-FK shape + naming-convention render, enum columns render `VARCHAR(32)`. (FR-004, FR-008, FR-009, SC-008)
- [X] T010 [P] Unit test `tests/unit/test_rls_catalog.py`: `emit_rls_policy` renders ENABLE+FORCE + fail-closed `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true),'')::uuid)` DDL for all four tables. (FR-003, SC-004)
- [X] T011 [P] Unit test `tests/unit/test_pagination.py`: cursor encode/decode round-trip, malformed cursor → `InvalidCursor`, `clamp_limit` caps at 500 and defaults to 50; **[analyze F3] envelope builder: given `limit+1` fetched rows → `next_cursor` is set (and only `limit` items returned); given ≤`limit` rows → `next_cursor` is null** (SC-009 more/none branch). (FR-015, SC-009)
- [X] T012 [P] Unit test `tests/unit/test_migration_offline_catalog.py`: `alembic upgrade head --sql` renders the four tables + partial indexes + RLS statements; assert single head. (FR-001, FR-003)
- [X] T013 [P] Extend `tests/unit/test_import_boundaries.py` to assert `app_shared.models.catalog`, `app_shared.pagination`, and `app_shared.catalog.*` import cleanly with **no** fastapi / scrapy / twisted / playwright imports.

**Checkpoint**: Models + migration + pagination in place; DB-independent shape/RLS/pagination tests green. User stories can begin.

---

## Phase 3: User Story 1 - Create and manage products and variants (Priority: P1) 🎯 MVP

**Goal**: Product CRUD + variant read/update, with every product guaranteed ≥1 variant (auto default when none given), variant-level money validated.

**Independent Test**: Create a simple product → exactly one default variant carrying its price/currency/sku/url; create a product with explicit variants → those exist, no spurious default; read/update/list products & variants and confirm workspace-scoped persistence; delete reports outcome.

### Core logic (DB-independent) + tests

- [X] T014 [P] [US1] Create `libs/shared/app_shared/catalog/default_variant.py`: pure `derive_default_variant(product) -> dict` (title = product title else `"Default"`; inherits sku/url; price/currency from payload; `option_values=None`; `status=active`) and `ensure_at_least_one(variants, product)`. (FR-005, FR-006)
- [X] T015 [P] [US1] Unit test `tests/unit/test_default_variant.py`: title default + fallback, sku/url/price/currency inheritance, `ensure_at_least_one` returns given variants unchanged when non-empty else one derived default. (FR-005, FR-006, SC-001)

### Schemas + endpoints

- [X] T016 [US1] Create `apps/api/app/schemas/catalog.py` product/variant DTOs: `ProductCreate` (title req; optional external_id/sku/brand/barcode/url/status/price/currency/variants), `VariantCreate`, `ProductResponse` (with variants), `VariantResponse`, `ProductUpdate`/`VariantUpdate` (PATCH), `{items, next_cursor}` list envelopes, and the `{id, outcome}` delete-outcome model. Money-as-Decimal finite + `NUMERIC(18,4)` scale validator and 3-letter `currency` validator reject at the boundary. (FR-004, FR-007, FR-017, SC-007)
- [X] T017 [US1] Create `apps/api/app/routers/products.py` (per contracts/api-products.md): `POST /v1/products` (require `products:write`; apply `derive_default_variant`/`ensure_at_least_one` when no variants, seed price/currency; single transaction; 422 when simple product lacks price/currency), `GET /v1/products` (require `products:read`; keyset pagination via `pagination.py`), `GET /v1/products/{id}` (`scoped_get` → 404 cross-ws), `PATCH /v1/products/{id}` (`products:write`), `DELETE /v1/products/{id}` (`products:write`; hard-delete now, structured for archive, returns `{id, outcome}`). Uses `scoped_select`/`scoped_get`. (FR-005, FR-014, FR-015, FR-016, FR-017)
- [X] T018 [US1] Create `apps/api/app/routers/variants.py` (per contracts/api-variants.md): `GET /v1/variants` (require `variants:read`; optional workspace-scoped `product_id` filter; paginated), `GET /v1/variants/{id}` (404 cross-ws), `PATCH /v1/variants/{id}` (require `variants:write`; money/currency boundary-validated; `title` change keeps `unique(workspace_id, product_id, title)` → 409). (FR-007, FR-014, FR-016) [analyze F2] Note: no variant-DELETE endpoint exists in this feature, so PATCH cannot drop a product to zero variants; the FR-006 last-variant invariant is a structural guard maintained by the catalog service (ensure_at_least_one, unit-tested in T009/T010) — it is NOT a runtime check on this PATCH. Do not add an untestable zero-variant 409 path here.
- [X] T019 [US1] Register the products and variants routers in `apps/api/app/main.py` under `/v1`. (FR-014)
- [ ] T020 [P] [US1] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_products_crud_live.py`: create simple product → 1 default variant persisted (price/currency/sku/url inherited); create with N explicit variants → N variants, 0 spurious default; read/update/list persistence; delete returns `hard_deleted`; last-variant invariant on a real DB. (SC-001)

**Checkpoint**: Products/variants CRUD + default-variant guarantee functional; MVP demoable (unit-verified here; live create deferred).

---

## Phase 4: User Story 2 - Bulk-upsert a Woo/Salla-style catalog payload (Priority: P1)

**Goal**: Set-based, idempotent bulk upsert of products + nested variants — bounded statements, identity resolution `external_id → sku → (product_id, title)`, in-batch last-wins, every upserted product ends with ≥1 variant.

**Independent Test**: Bulk-upsert a payload → all created; re-push with changes → matched rows updated in place, 0 duplicates; runs as a bounded number of statements regardless of batch size; two in-batch records with the same identity resolve last-wins without error.

### Core logic (DB-independent) + tests

- [X] T021 [P] [US2] Create `libs/shared/app_shared/catalog/consistency.py`: pure workspace-consistency pre-check helpers (referenced product/variant/group ids must resolve in-workspace; cross-workspace/nonexistent rejected) operating on plain id sets/maps. (FR-009)
- [X] T022 [P] [US2] Unit test `tests/unit/test_workspace_consistency.py`: accepts in-workspace refs, rejects a cross-workspace ref and a nonexistent ref. (FR-009)
- [X] T023 [US2] Create `libs/shared/app_shared/catalog/upsert.py` (pure, compiles statements — no execution): `resolve_identity(row)` (order external_id → sku → variants `(product_id, title)`), partition batch by identity kind, `dedup_last_wins(rows, key)`, `build_products_upsert`/`build_variants_upsert` → `pg_insert(...).on_conflict_do_update(index_elements=..., index_where=text("... IS NOT NULL"), set_={excluded...})` (≤3 statements/table), set-based variant→product parent resolution via one `IN (...)` lookup, default-variant injection for products arriving with zero variants, and always-insert handling for identity-less products (neither external_id nor sku). (FR-010, FR-011, FR-012)
- [X] T024 [P] [US2] Unit test `tests/unit/test_catalog_upsert.py`: compile to `postgresql` dialect SQL and assert `ON CONFLICT (...) WHERE ... DO UPDATE SET ...` per identity kind (correct partial/full index), identity resolution order, `dedup_last_wins` keeps last, bounded statement count (≤3/table, no per-row loop), identity-less product always-insert. (FR-010, FR-011, FR-012, SC-002, SC-003)
- [X] T025 [US2] Extend `apps/api/app/schemas/catalog.py` with bulk-upsert payload DTOs: `ProductBulkUpsert` (Woo/Salla nested — products each carrying optional nested `variants`), `VariantBulkUpsert`, and their result envelopes; reuse the money/currency validators. (FR-010, FR-011, SC-007)
- [X] T026 [US2] Add `POST /v1/products/bulk-upsert` (require `products:write`) to `apps/api/app/routers/products.py`: dedup last-wins, resolve identity, build set-based statements via `upsert.py`, inject default variants, run under the request workspace context. (FR-010, FR-011, FR-012, FR-014)
- [X] T027 [US2] Add `POST /v1/variants/bulk-upsert` (require `variants:write`) to `apps/api/app/routers/variants.py`: set-based variant upsert with parent-product resolution + workspace-consistency pre-check (reject cross-workspace/unresolvable parent). (FR-009, FR-010, FR-011, FR-014)
- [ ] T028 [P] [US2] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_bulk_upsert_live.py`: insert batch then re-push unchanged → 0 duplicates; re-push changed → in-place update (matched external_id → sku → product+title); in-batch same-identity → last-wins; each upserted product ends with ≥1 variant on a real DB. (SC-002)

**Checkpoint**: Idempotent set-based bulk upsert verified via compiled SQL here; live idempotency deferred.

---

## Phase 5: User Story 4 - Catalog access is workspace-isolated and scope-gated (Priority: P1)

**Goal**: Prove end-to-end isolation + scope enforcement: no cross-workspace read/write (app filter + RLS fail-closed), read-scoped credential cannot write, write-scoped can, and the CI guard flags any unscoped catalog query.

**Independent Test**: Two populated workspaces → workspace-A caller reads/writes 0 of workspace-B's rows (including when the app filter is omitted, and 0 rows with no context set); read-only credential write → refused, write credential → succeeds; scoping guard fails on an introduced unscoped `select(Product)`.

- [ ] T029 [P] [US4] Unit test `tests/unit/test_catalog_scoping_guard.py`: `scripts/check_workspace_scoping.py` exits 0 on the current tree (four models registered) AND flags a planted unscoped `select(Product)` (and `scoped_get` omission). (FR-002, SC-006)
- [ ] T030 [P] [US4] Unit test `tests/unit/test_catalog_scope_gating.py`: each catalog route declares the correct `require_scopes` (read vs write per family) — assert via app route/dependency inspection AND a `TestClient` call with a fake principal lacking the scope → 403. (FR-016, SC-005)
- [ ] T031 [P] [US4] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_workspace_isolation_live.py`: workspace-A caller gets 0 rows of workspace-B catalog by id and in lists; app-filter-omitted query returns 0 other-workspace rows (RLS); no-context → 0 rows (fail closed); read-only credential write → 403, write credential → 200; composite-FK cross-workspace reference rejected. (FR-003, FR-016, SC-004, SC-005)

**Checkpoint**: Scope-gating + guard proven here; live cross-workspace/RLS row denial deferred.

---

## Phase 6: User Story 3 - Group products and variants (Priority: P2)

**Goal**: Named product groups with add/remove membership (product or variant), workspace-scoped, duplicate membership rejected, references resolve in-workspace.

**Independent Test**: Create a group; add a product and a variant; list groups; remove an item; confirm workspace-scoped membership and that re-adding the same product/variant is rejected.

- [ ] T032 [US3] Extend `apps/api/app/schemas/catalog.py` with group DTOs: `GroupCreate` (name req), `GroupUpdate`, `GroupResponse` (with items), `GroupItemCreate` (exactly one of `product_id` | `product_variant_id`), `GroupItemResponse`, `{items, next_cursor}` group list envelope, delete-outcome. (FR-013)
- [ ] T033 [US3] Create `apps/api/app/routers/product_groups.py` (per contracts/api-product-groups.md): `POST/GET/GET{id}/PATCH/DELETE /v1/product-groups` and `POST /v1/product-groups/{id}/items` + `DELETE /v1/product-groups/{id}/items/{item_id}`. Scope mapping — group create/update/delete + add/remove require `products:write` (add-variant item also `variants:write`), reads require `products:read`. `unique(workspace_id, name)` dup → 409; duplicate membership (partial unique) → 409; item reference resolved in-workspace via `consistency.py` → 422/404 on cross-ws/nonexistent; DELETE returns `{id, outcome}`. (FR-009, FR-013, FR-014, FR-016, FR-017)
- [ ] T034 [US3] Register the product-groups router in `apps/api/app/main.py` under `/v1`. (FR-014)
- [ ] T035 [P] [US3] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_groups_live.py`: create group + add product/variant items; re-adding same item rejected (duplicate membership); remove item; all references workspace-local on a real DB. (SC-008)

**Checkpoint**: Grouping functional (unit + schema level); live membership uniqueness deferred.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T036 [P] Document the identity-less-product always-insert limitation (callers must supply external_id/sku to upsert) in the bulk-upsert schema/router docstrings in `apps/api/app/schemas/catalog.py` and `apps/api/app/routers/products.py`. (FR-011)
- [ ] T037 Run the DB-independent validation from `specs/004-catalog-products-variants-groups/quickstart.md`: full `tests/unit` suite green + `scripts/check_workspace_scoping.py` exit 0 + `scripts/check_single_head.sh` single head + import-boundary green.
- [ ] T038 [P] ⏸ DEFERRED (needs live Postgres) Run the online migration on a Postgres host: `alembic upgrade head` creates the four tables + partial indexes + RLS; `alembic downgrade` reverses cleanly. (FR-001)
- [ ] T039 [P] ⏸ DEFERRED (needs live Postgres) Execute the live-DB section of `specs/004-catalog-products-variants-groups/quickstart.md` (end-to-end request flows: scope refusal 403 / write 200, create→default-variant, bulk idempotency, group membership, cross-workspace + RLS denial). (SC-002, SC-004, SC-005, SC-008)

---

## FR / SC Coverage

| Requirement | Task(s) |
|-------------|---------|
| FR-001 four workspace-owned tables + RLS in creating migration | T004, T008, T009, T010, T012, T038 |
| FR-002 registered workspace-owned + CI guard covers them | T006, T029 |
| FR-003 cross-ws blocked by app scoping AND RLS (fail closed) | T008, T010, T031 |
| FR-004 product/variant field shapes | T004, T009, T016 |
| FR-005 auto default variant on create; explicit → no extra | T014, T015, T017 |
| FR-006 product always retains ≥1 variant | T014, T015, T018 |
| FR-007 finite NUMERIC(18,4) money + 3-letter currency | T004, T016 |
| FR-008 uniqueness rules (partial + full uniques) | T004, T008, T009 |
| FR-009 all FK references resolve in-workspace | T004, T008, T021, T022, T027, T033 |
| FR-010 set-based bulk upsert, bounded statements | T023, T024, T026, T027 |
| FR-011 identity order + identity-less always-insert | T023, T024, T025, T036 |
| FR-012 in-batch last-wins + ≥1 variant in bulk | T023, T024, T026 |
| FR-013 groups CRUD + items + duplicate membership rejected | T032, T033 |
| FR-014 catalog endpoints under /v1, nothing outside scope | T017, T018, T019, T026, T027, T033, T034 |
| FR-015 cursor pagination default 50 / max 500 | T007, T011, T017 |
| FR-016 workspace context + scope-gating per family | T017, T018, T026, T027, T030, T033 |
| FR-017 delete hard-vs-archive + response indicates outcome | T016, T017, T033 |
| SC-001 simple → 1 variant; N explicit → N | T015, T020 |
| SC-002 re-push 0 dupes / in-place update | T024, T028, T039 |
| SC-003 bounded statements (no row loops) | T024 |
| SC-004 two-workspace 0 rows, RLS, no-context fail closed | T010, T031, T039 |
| SC-005 read-only 0 writes; write succeeds | T030, T031, T039 |
| SC-006 CI guard fails on introduced unscoped query | T029 |
| SC-007 100% money finite + valid currency at boundary | T016, T025 |
| SC-008 uniqueness holds (products/variants/groups/members) | T009, T035, T039 |
| SC-009 lists ≤500/page + cursor when more | T007, T011 |

Every FR-001..FR-017 and SC-001..SC-009 maps to ≥1 task.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: depends on Setup (T001 enums used by models); **blocks all user stories**.
- **US1 (Phase 3)**, **US2 (Phase 4)**, **US4 (Phase 5)**, **US3 (Phase 6)**: each depends on Foundational. US2/US4 build on the routers/schemas seeded in US1 (T016/T017/T018), so run US1 first; US4 tests the routers from US1–US3; US3 is independent of US2. Recommended order by priority: US1 → US2 → US4 → US3.
- **Polish (Phase 7)**: after the desired stories.

### Within a story

- Core (`app_shared/catalog/*`) + its unit test before the router that uses it.
- `schemas/catalog.py` before/with the routers that import it.
- Router file before its `main.py` registration.
- Deferred (⏸) tasks are authored anytime but only pass on a Postgres host.

### Parallel Opportunities

- Setup: T001, T002, T003 in parallel.
- Foundational: T007 (pagination) parallel with T004–T006; unit tests T009–T013 parallel once their targets exist.
- US1: T014/T015 (core+test) parallel; T020 authored in parallel.
- US2: T021/T022 parallel with T023/T024 authoring; T028 parallel.
- US4: T029, T030, T031 all parallel.
- US3: T035 parallel with T032–T034.

---

## Parallel Example: Foundational unit tests

```bash
# After T004–T008 land, run the DB-independent shape/RLS/pagination/migration tests together:
Task: "Unit test tests/unit/test_catalog_models.py"
Task: "Unit test tests/unit/test_rls_catalog.py"
Task: "Unit test tests/unit/test_pagination.py"
Task: "Unit test tests/unit/test_migration_offline_catalog.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (models + migration + pagination, all shape/RLS tests green).
2. Phase 3 US1 → create/manage products + variants with the default-variant guarantee.
3. **STOP & VALIDATE**: run the unit suite; author the deferred live CRUD test for the PG host.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (MVP) → US2 (bulk upsert) → US4 (isolation/scope proof) → US3 (groups).
3. Polish: docs of the identity-less limitation, quickstart unit validation; run the DEFERRED live-DB + online-migration tasks on a Postgres-capable host.

### Deferred (live-Postgres) tasks

T020, T028, T031, T035, T038, T039 — authored here, left unchecked `- [ ]`, marked ⏸ DEFERRED (needs live Postgres). They cover the live halves of SC-001, SC-002, SC-004, SC-005, SC-008 and the online migration run.

---

## Notes

- `[P]` = different files, no incomplete-task dependency.
- `app_shared` stays FastAPI-free and scrapy/twisted/playwright-free (T013 guards this); Pydantic API DTOs live in `apps/api` (research D7).
- Do NOT commit — the orchestrator commits after this step.
