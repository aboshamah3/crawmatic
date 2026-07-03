# Implementation Plan: Catalog — Products, Variants & Groups

**Branch**: `004-catalog-products-variants-groups` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-catalog-products-variants-groups/spec.md`

## Summary

Deliver the first real **business-data** slice on top of the SPEC-02 DB foundation and the SPEC-03 isolation/auth machinery: the four workspace-owned **catalog** tables (`products`, `product_variants`, `product_groups`, `product_group_items`), their `/v1` CRUD + **set-based bulk-upsert** + grouping endpoints, the **default-variant** guarantee (every product always has ≥1 variant), and the first **end-to-end scope-gated** API surface (`products:read/write`, `variants:read/write` via the SPEC-03 `require_scopes` seam).

Concretely this feature adds:
- `app_shared`: `models/catalog.py` (four ORM models on `WorkspaceScopedBase`, with **partial** unique indexes and workspace-local **composite** FKs), new status enums in `enums.py`, a framework-agnostic `pagination.py` (opaque keyset cursor encode/decode + limit cap), a framework-agnostic `catalog/` core package (pure default-variant derivation, set-based upsert statement builder + identity resolution + in-batch last-wins dedup, workspace-consistency check helpers), the four models registered in `models/__init__.py` and added to `WORKSPACE_OWNED_MODELS` in `repository.py`, and a generalized repository `ModelT` bound.
- `apps/api`: `schemas/catalog.py` (Pydantic request/response DTOs — FastAPI-coupled, so they live here not in `app_shared`), routers `products.py` / `variants.py` / `product_groups.py` (scope-gated via `deps.require_scopes`, using the scoped repository helpers on the already-context-set request session), a small `pagination` wiring shim, and their registration in `main.py`.
- repo root: one Alembic migration creating the four tables (exact §22 shapes, partial unique indexes, composite workspace-local FKs) and calling `emit_rls_policy` on **all four** in the same migration; `down_revision = 55da7d6d939d` (current head).

Everything DB-independent is fully unit-tested **here** (model/constraint/index shapes + naming render, partial-index `postgresql_where` render, RLS DDL render for the four tables, default-variant derivation, bulk-upsert statement construction + `on_conflict_do_update` inference + identity ordering + last-wins dedup compiled to SQL without executing, cursor encode/decode round-trip + max-limit cap, workspace-consistency check logic, scope-gating wiring, and the CI scoping guard still passing with the four new models). Live-Postgres items (actual create/upsert, RLS row denial, cross-workspace blocking, migration online run, end-to-end request flows) are **authored and marked** for a PG-capable host — no Docker daemon / live Postgres in this build env.

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**:
- Existing only — no new third-party deps. SQLAlchemy 2.0 (sync) incl. the **PostgreSQL dialect** `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)` for set-based upsert; psycopg 3; Alembic; FastAPI + Pydantic v2 (`apps/api`); stdlib `base64`/`json` for the opaque cursor.
- `app_shared` MUST NOT import FastAPI (framework-agnostic) and MUST NOT import scrapy/twisted/playwright (unchanged import-boundary test). Models, Money, enums, the pagination helper, and the bulk-upsert **core** live in `app_shared`; the FastAPI schemas/routers/deps live in `apps/api`.

**Storage**: PostgreSQL 17. App requests connect through PgBouncer (transaction pooling); the migration connects directly (`MIGRATION_DATABASE_URL`). Workspace context is set per-transaction by the SPEC-03 auth seam (`set_config('app.workspace_id', :wsid, true)`) before any catalog query. Partial unique **indexes** (`Index(..., postgresql_where=...)`) back the "unique where not null" rules; RLS enabled+forced on all four tables in the creating migration.

**Testing**: pytest. DB-independent logic unit-tested here (compile-to-SQL for the upsert statement, no execution). Live-DB items authored and skipped when no reachable Postgres is present (same pattern as SPEC-03 `test_migration_offline_auth.py` / live markers).

**Target Platform**: Linux server / containers. Only `apps/api` is publicly exposed.

**Project Type**: Backend monorepo (uv workspace). Spans `app_shared` (models, pagination, catalog core) and `apps/api` (schemas, routers), plus repo-root Alembic.

**Performance Goals**: Bulk-upsert executes in a **bounded** number of statements regardless of batch size (never one statement per record — SC-003): one `INSERT ... ON CONFLICT DO UPDATE` per target table (products, then variants), plus the fixed lookups needed to resolve variant→product identity. List endpoints use **keyset** cursor pagination (indexed `(created_at, id)` seek — no OFFSET scans), default 50 / max 500 (FR-015/SC-009). No per-row loops on any ingestion path (Principle VIII).

**Constraints**: Transaction-pooling-safe only (no server-side prepared statements — already `prepare_threshold=None`; only `SET LOCAL`/`set_config(...,true)`; no session advisory locks). RLS fails closed (zero rows) when no workspace context is set. Money is finite `NUMERIC(18,4)` via the SPEC-02 `Money` type; `currency` is `char(3)`; no cross-currency comparison in v1. `app_shared` stays FastAPI-free and scrapy-free. No live Postgres in this build env.

**Scale/Scope**: Foundation for 2,000 products / 10k–20k matches per workspace (§39). This spec adds **exactly four** tables and the catalog `/v1` endpoints + default-variant + isolation. **No** competitors / matches / scrape-profiles / observations / prices / alerts (SPEC-05+); the catalog tables carry no references to those yet.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | New `app_shared` modules + FastAPI in `apps/api` | Catalog ORM models, pagination helper, and the bulk-upsert/default-variant/workspace-consistency **core** live in `app_shared` and import only sqlalchemy/stdlib — never fastapi, never scrapy/twisted/playwright. The import-boundary test is extended to cover `app_shared.models.catalog`, `app_shared.pagination`, and `app_shared.catalog.*`. Pydantic request/response schemas + the `/v1` routers + scope-gating deps live in `apps/api`, importing `app_shared` one-way. Only `apps/api` is publicly exposed. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | Core of this spec | All four tables use `WorkspaceScopedBase` (`workspace_id NOT NULL`); `emit_rls_policy()` (ENABLE + FORCE + fail-closed `NULLIF(current_setting('app.workspace_id', true),'')::uuid`) is called on **all four** in the **same** creating migration. All four are added to `WORKSPACE_OWNED_MODELS`, so the scoped repository helpers cover them and the AST CI guard (`scripts/check_workspace_scoping.py`, which imports that set) fails the build on any introduced unscoped fetch/select — verified still green with the four new models. Every endpoint runs on the SPEC-03 auth-seam session that has already called `set_workspace_context`; reads/writes go through `scoped_select`/`scoped_get`. All FKs are **workspace-local** (composite `(workspace_id, <ref>_id)` → parent `(workspace_id, id)`; parents get a `unique(workspace_id, id)`), so a cross-workspace reference is structurally impossible, not just app-checked. Cross-workspace + no-context (fail-closed) tests authored (live-DB). **PASS** |
| **III. Variant-Level Pricing & Explicit Matching** | Directly exercised | Pricing lives **only** on `product_variants` (`current_price`/`currency`); `products` has **no** price column. Every product — including a "simple" one — gets exactly one **default variant** (create + bulk-upsert), and a product must always retain ≥1 variant (an update that would remove the last variant is prevented / re-establishes a default). No competitor-match surface here (SPEC-05+), consistent with "matching is variant-level and explicit". **PASS** |
| **IV. Database-driven config** | Light | No hardcoded scrape/threshold behavior added. Pagination limits (default 50 / max 500) are constants from §24, not per-row DB config. No scrape-profile/access-policy coupling (later specs). **PASS (N/A-ish)** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | Import boundary only | No scraping code; `app_shared` stays scrapy/twisted/playwright-free (see I). **PASS (N/A)** |
| **VI. Internal-only / legal** | N/A this spec | No scraping/access code. **PASS (N/A)** |
| **VII. Monetary & Extraction Correctness (NON-NEGOTIABLE)** | Variant money | `current_price` uses the SPEC-02 `Money` type (finite `NUMERIC(18,4)`, rejects float/NaN/Inf/over-scale at the boundary); `currency` is a validated 3-letter code (`char(3)`). No cross-currency comparison performed (values only stored, per §19). No confidence/alert logic in this spec. **PASS** |
| **VIII. Scale-Safe Data & Concurrency (NON-NEGOTIABLE)** | Ingestion & lists | Bulk upsert is **set-based** — one `INSERT ... ON CONFLICT DO UPDATE` per target table, no per-row loop (SC-003), constructed by a pure builder and compiled-to-SQL in tests. Keyset (`created_at, id`) cursor pagination — no OFFSET table scans; capped at 500. UUIDv7 PKs (via `Base`) keep inserts index-friendly at scale; `TIMESTAMPTZ` everywhere (via `TimestampMixin`/`TZDateTime`). All app traffic through PgBouncer; only `SET LOCAL`/`set_config(...,true)` (the auth seam), no session advisory locks. Single linear migration history (existing CI head guard). Partitioning/retention (§29) is N/A — catalog tables are mutable-state, not append-heavy. **PASS** |

**Technology & Security Constraints (§24/§33/§34)**: Stack lock-in honored (SQLAlchemy+Alembic, PostgreSQL pg-dialect `insert`, psycopg, FastAPI/Pydantic). Public API versioned under `/v1`; list endpoints cursor-paginated default 50 / max 500 (§24). UUIDv7 public ids (§21). Deletion follows §24 mutating rules: hard-delete only while no dependent history exists (true now), structured for archive-by-status, response indicates which outcome (FR-017). Structured error-code vocabulary reused where relevant (e.g. `FORBIDDEN`/`NOT_FOUND`/validation `422`); no new secrets. Product-group management reuses `products`/`variants` write scopes (no group scope exists in the §33 vocabulary — documented mapping).

**Gate result**: PASS — no violations. Complexity Tracking table intentionally empty. Re-checked post-Phase-1 (see end of plan): still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/004-catalog-products-variants-groups/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 — model location + status enums, partial-unique-index + ON CONFLICT
│                        #   inference, default-variant derivation, set-based upsert design + identity order
│                        #   + last-wins, keyset cursor design, FK workspace-consistency, schemas location,
│                        #   migration head, unit-vs-live test split
├── data-model.md        # Phase 1 — 4 tables (exact §22 shapes), enums, partial uniques, composite FKs,
│                        #   RLS/isolation model, default-variant + last-variant invariants, upsert identity
├── quickstart.md        # Phase 1 — how to validate (unit here; live create/upsert/RLS/migration on a PG host)
├── contracts/           # Phase 1 — interfaces this feature exposes
│   ├── api-products.md        # /v1/products CRUD + bulk-upsert
│   ├── api-variants.md        # /v1/variants list/get/update + bulk-upsert
│   ├── api-product-groups.md  # /v1/product-groups CRUD + add/remove item
│   ├── models-catalog.md      # ORM model shapes, partial uniques, composite FKs, enums
│   ├── catalog-bulk-upsert.md # set-based upsert core: statement builder, identity order, last-wins, ON CONFLICT
│   ├── default-variant.md     # pure default-variant derivation + ≥1-variant invariant
│   ├── pagination.md          # opaque keyset cursor encode/decode + limit cap
│   ├── workspace-consistency.md # composite-FK workspace-local reference rule + app pre-check
│   └── migration-catalog.md   # the catalog migration + RLS on all four + single head
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py             # EXTEND: ProductStatus, VariantStatus, GroupStatus (StrEnum);
│                        #   product_group_items has no status column (§22) → no GroupItemStatus.
│                        #   RecordStatus (active/archived) reused as the value set; new named enums
│                        #   alias it per-entity for clarity + future divergence (documented in research D1).
├── pagination.py        # NEW: encode_cursor((created_at, id)) / decode_cursor(str) (base64(json)); LimitSpec
│                        #   (DEFAULT=50, MAX=500, clamp); keyset predicate builder helper. Framework-agnostic.
├── repository.py        # EXTEND: add the 4 catalog models to WORKSPACE_OWNED_MODELS; widen ModelT bound
│                        #   (constrained TypeVar → `TypeVar("ModelT", bound=Base)`) so scoped helpers type the
│                        #   new models. Helper behavior unchanged.
├── models/
│   ├── __init__.py      # EXTEND: re-export Product, ProductVariant, ProductGroup, ProductGroupItem
│   └── catalog.py       # NEW: the 4 ORM models (WorkspaceScopedBase + TimestampMixin where §22 has
│                        #   created_at/updated_at; ProductGroupItem has created_at only), partial unique
│                        #   Index(..., postgresql_where=...), composite workspace-local FKs +
│                        #   unique(workspace_id, id) on products/product_variants/product_groups parents.
└── catalog/
    ├── __init__.py      # NEW
    ├── default_variant.py # NEW: pure derive_default_variant(product_fields) -> variant_fields
    │                      #   (title default "Default", inherits price/currency/sku/url); ensure_at_least_one
    ├── upsert.py         # NEW: build_products_upsert(rows) / build_variants_upsert(rows) → pg insert stmt
    │                      #   with on_conflict_do_update targeting the right (partial) index; identity
    │                      #   resolution order external_id→sku→(product_id,title); dedup_last_wins(rows, key)
    └── consistency.py    # NEW: pure workspace-consistency check helpers (referenced ids resolve in-workspace)

apps/api/app/
├── main.py              # EXTEND: include the products / variants / product-groups routers
├── schemas/
│   ├── __init__.py      # NEW
│   └── catalog.py       # NEW: Pydantic request/response DTOs (Money as Decimal, currency validator,
│                        #   {items, next_cursor} list envelope, bulk-upsert payloads, delete-outcome response)
└── routers/
    ├── products.py       # NEW: POST/GET/GET{id}/PATCH/DELETE /v1/products + POST /v1/products/bulk-upsert
    │                      #   (require_scopes products:read|write); create applies default variant
    ├── variants.py       # NEW: GET/GET{id}/PATCH /v1/variants + POST /v1/variants/bulk-upsert
    │                      #   (require_scopes variants:read|write); last-variant invariant enforced
    └── product_groups.py # NEW: POST/GET/PATCH/DELETE /v1/product-groups + add/remove item
                          #   (require_scopes products:write|variants:write per documented mapping)

alembic/versions/
└── <rev>_catalog_tables.py   # NEW: create products, product_variants, product_groups, product_group_items
                              #   (exact §22 shapes, partial unique indexes, composite workspace-local FKs +
                              #   unique(workspace_id, id) parents); emit_rls_policy on all four; downgrade;
                              #   down_revision = 55da7d6d939d (current head)

tests/unit/
├── test_import_boundaries.py    # EXTEND: cover app_shared.models.catalog + app_shared.pagination + app_shared.catalog.*
├── test_catalog_models.py       # NEW: table/column shapes, partial-index postgresql_where render, composite FKs, enums
├── test_rls_catalog.py          # NEW: emit_rls_policy render for all four tables (fail-closed DDL)
├── test_default_variant.py      # NEW: derivation (title default, inheritance) + ≥1-variant invariant
├── test_catalog_upsert.py       # NEW: build_*_upsert compiles to expected ON CONFLICT DO UPDATE SQL; identity
│                                 #   order; last-wins dedup; one statement per table (no per-row loop)
├── test_pagination.py           # NEW: cursor encode/decode round-trip; malformed cursor rejected; limit clamp to 500
├── test_workspace_consistency.py# NEW: consistency helper accepts in-workspace refs, rejects cross/nonexistent
├── test_catalog_scoping_guard.py# NEW: CI guard flags a planted select(Product) w/o workspace_id; clean passes
├── test_catalog_scope_gating.py # NEW: routers declare the correct require_scopes (read vs write per family)
└── test_migration_offline_catalog.py # NEW: `alembic upgrade head --sql` renders 4 tables + partial indexes + RLS; single head

tests/integration/  (authored, live-DB-marked — skipped without Postgres)
├── test_products_crud_live.py       # create→default variant; read/update/list/delete outcome
├── test_bulk_upsert_live.py         # insert then re-push: 0 dupes, in-place update, last-wins
├── test_groups_live.py              # group + items; duplicate membership rejected
└── test_workspace_isolation_live.py # cross-workspace blocked (app + RLS); no-context → 0 rows; scope refusal
```

**Structure Decision**: Backend monorepo (uv workspace), matching SPEC-03. `app_shared` gains catalog models, a framework-agnostic pagination helper, and a framework-agnostic `catalog/` core (pure default-variant/upsert/consistency logic) so all DB-independent behavior is unit-testable without FastAPI or a live DB. FastAPI request/response **schemas**, routers, and scope-gating deps live in `apps/api` (keeping `app_shared` FastAPI-free). The four tables + RLS land in one repo-root Alembic migration chained onto the current head `55da7d6d939d`.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
