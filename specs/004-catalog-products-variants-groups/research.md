# Phase 0 Research: Catalog — Products, Variants & Groups

All decisions below were resolvable from `PROJECT_SPEC.md` (§19/§21/§22/§24/§32), the Constitution (v1.0.1, esp. II/III/VII/VIII), and the SPEC-02/03 foundation already in the tree. No open `NEEDS CLARIFICATION` remained. Each decision records the chosen approach, rationale, and rejected alternatives.

---

## D1 — Model location & status enums

**Decision.** The four ORM models live in a single new module `libs/shared/app_shared/models/catalog.py` (`Product`, `ProductVariant`, `ProductGroup`, `ProductGroupItem`), re-exported from `models/__init__.py` (so `Base.metadata` sees them for Alembic offline render and callers can `from app_shared.models import Product`). All four use `WorkspaceScopedBase` (`workspace_id NOT NULL`) — they are unconditionally workspace-owned (no SUPER_ADMIN nullable-workspace special case that `users` had). `Product`, `ProductVariant`, `ProductGroup` add `TimestampMixin` (they have `created_at`+`updated_at`); `ProductGroupItem` declares `created_at` directly as `TZDateTime` (§22 shape has **no** `updated_at`), exactly like `RefreshToken` in `identity.py`.

Status enums go in `enums.py` as `ProductStatus`, `VariantStatus`, `GroupStatus` (each `StrEnum` with values `active` / `archived`). `product_group_items` has **no** `status` column in §22, so there is **no** `GroupItemStatus`. The three enums share the same value set as the existing `RecordStatus(active|archived)`; we still declare **named per-entity enums** (rather than reusing `RecordStatus` directly) so each entity's lifecycle can diverge later (e.g. a variant `discontinued` state) without a churny rename, and so column types read self-documentingly. `active`/`archived` directly serves the FR-017 archive-by-status delete path.

**Rationale.** Mirrors the SPEC-03 `identity.py` pattern exactly (one module per table family, `TimestampMixin` vs bare `created_at`, string-backed app-validated enums via `enum_column`). Keeps models in `app_shared` (framework-agnostic) so Alembic and unit tests reach them without FastAPI.

**Alternatives rejected.** (a) One file per model — needless fragmentation; the identity precedent is one file per family. (b) Reusing `RecordStatus` for all three columns — couples three independent lifecycles to one enum; a later per-entity state would force a wide change. (c) A Postgres-native ENUM — forbidden by the established `app_shared.enums` convention (string-backed, app-validated only).

---

## D2 — Partial unique indexes + the exact `ON CONFLICT` inference

**Decision.** The "unique **where not null**" rules (products/variants on `external_id` and on `sku`) are implemented as **partial unique indexes** using `Index("uq_...","workspace_id","external_id", unique=True, postgresql_where=text("external_id IS NOT NULL"))` — a plain `UniqueConstraint` cannot carry a `WHERE`. The always-on rules use partial-free indexes/constraints: `product_variants` `unique(workspace_id, product_id, title)`, `product_groups` `unique(workspace_id, name)`, and `product_group_items` `unique(workspace_id, product_group_id, product_id)` / `unique(workspace_id, product_group_id, product_variant_id)` (both are partial-safe as written since Postgres treats NULLs as distinct — a group_item referencing a variant has `product_id IS NULL`, so the product-uniqueness index simply doesn't constrain it; documented, no explicit `WHERE` needed but we add `postgresql_where=... IS NOT NULL` on the two group-item indexes for intent + to avoid multiple all-NULL rows colliding under future `NULLS NOT DISTINCT`).

**`ON CONFLICT` inference against a partial index.** Postgres index-inference for `ON CONFLICT` requires the inference specification to include the **same predicate** as the partial index. So the bulk-upsert statement targeting the `external_id` partial index is:

```sql
INSERT INTO products (...) VALUES (...)
ON CONFLICT (workspace_id, external_id) WHERE external_id IS NOT NULL
DO UPDATE SET ...
```

In SQLAlchemy this is `pg_insert(Product).on_conflict_do_update(index_elements=["workspace_id","external_id"], index_where=text("external_id IS NOT NULL"), set_={...})`. The `index_where` is **mandatory** to match a partial index — without it Postgres raises *"there is no unique or exclusion constraint matching the ON CONFLICT specification"*. This is unit-verifiable by compiling the statement to its SQL string (dialect=`postgresql`) and asserting the `ON CONFLICT ... WHERE ... DO UPDATE` text — no live DB needed.

**Which index a given upsert targets** (identity precedence, see D4): a single `INSERT` statement can only infer **one** conflict target, so the set-based upsert **partitions the batch by resolved identity kind** and issues one statement per (table, identity-kind) — i.e. rows keyed by `external_id` → statement inferring the `external_id` partial index; rows keyed by `sku` → statement inferring the `sku` partial index; variant rows keyed by `(product_id, title)` → statement inferring that full unique. This is still a **bounded** number of statements (≤3 per table), independent of row count — satisfying SC-003. Rows with neither `external_id` nor `sku` (and, for variants, resolved by `(product_id,title)`) always have a target; a product with none of the three still inserts (its `(workspace_id,id)` PK never conflicts on a fresh UUIDv7).

**Rationale.** Partial indexes are the only correct encoding of "unique only where present"; the mandatory `index_where` match is the real Postgres nuance the master doc flagged. Partitioning by identity kind keeps each statement's conflict target unambiguous while staying set-based.

**Alternatives rejected.** (a) `UniqueConstraint` (non-partial) — would forbid multiple NULL `external_id`/`sku` rows, wrong per §22. (b) A single mega-`INSERT` inferring all indexes — impossible; ON CONFLICT infers one arbiter. (c) Coalescing NULLs to a sentinel to use a non-partial unique — corrupts data and defeats "where not null". (d) Application-side "select then insert/update" loop — row-by-row, violates Principle VIII / SC-003.

---

## D3 — FK workspace-consistency: composite FKs (chosen) vs app-check

**Decision.** Use **composite foreign keys** including `workspace_id`. `product_variants(workspace_id, product_id)` → `products(workspace_id, id)`; `product_group_items(workspace_id, product_group_id)` → `product_groups(workspace_id, id)`; `product_group_items(workspace_id, product_id)` → `products(workspace_id, id)`; `product_group_items(workspace_id, product_variant_id)` → `product_variants(workspace_id, id)`. Each parent therefore needs a `unique(workspace_id, id)` (in addition to its `id` PK) so it can be the composite-FK target. The nullable group-item references (`product_id`, `product_variant_id`) use `ForeignKeyConstraint` with the nullable columns — Postgres `MATCH SIMPLE` (default) means a row with any NULL FK column is **not** checked, which is exactly right (a group item references *either* a product *or* a variant, never both required).

A lightweight **application-layer pre-check** (`app_shared/catalog/consistency.py`, pure) is *also* provided so the API can return a clean `422`/`404` ("referenced product not in this workspace") before hitting the DB, rather than surfacing a raw FK-violation `IntegrityError` — but the composite FK is the **structural** guarantee; the app-check is UX, not the enforcement of record.

**Rationale.** A composite FK makes a cross-workspace reference *structurally impossible* at the DB — strictly stronger than an app check and consistent with Principle II ("isolation is structural, not advisory"). The `unique(workspace_id, id)` parents are cheap (they subsume/align with the existing per-table PK). This also composes with RLS: even a BYPASSRLS path can't create a mismatched reference.

**Alternatives rejected.** (a) Single-column FK `product_variants.product_id → products.id` + app-only workspace check — a bug/omission in the app check silently allows a cross-workspace link; advisory not structural. (b) DB trigger enforcing same-workspace — more moving parts than a composite FK for the same guarantee. (c) No FK at all — rejected; loses referential integrity and the isolation guarantee.

---

## D4 — Bulk-upsert: set-based design, identity order, in-batch last-wins

**Decision.** The upsert **core** is pure and framework-agnostic (`app_shared/catalog/upsert.py`), unit-tested by compiling statements to SQL (never executing):

1. **Identity resolution order** (FR-011, §24): `external_id` → `sku` → (variants only) `(product_id, title)`. A per-row `resolve_identity(row)` picks the first present key; rows are then **partitioned by identity kind**.
2. **In-batch last-wins dedup** (FR-012): within each partition, `dedup_last_wins(rows, key)` collapses rows sharing a resolved identity keeping the **last** occurrence (stable, deterministic) — required because Postgres `ON CONFLICT` errors if a single `INSERT ... VALUES` contains two rows that hit the same arbiter ("ON CONFLICT DO UPDATE command cannot affect row a second time"). Dedup happens **before** statement build.
3. **Set-based statement** (FR-010, SC-003): one `pg_insert(Model).values([...all rows in partition...]).on_conflict_do_update(index_elements=..., index_where=..., set_={col: insert.excluded[col] for updatable cols})` per (table, identity-kind) partition — a **bounded** count (≤3 statements/table), never one-per-row.
4. **Variant→product resolution**: incoming variant rows reference their parent by the parent's identity (external_id/sku) or an explicit `product_id`; the service resolves parent product ids in **one** scoped `select` (set-based `WHERE (external_id, sku) IN (...)`), maps them, then builds the variant upsert. Unresolvable parent → row rejected (workspace-consistency, D3).
5. **Default-variant guarantee in bulk** (FR-012 tail): after the products upsert, any product in the batch that arrived with **zero** variants gets one derived default variant (D5) added to the variant upsert set — so every upserted product ends with ≥1 variant.

All of statement build + identity order + dedup is asserted by compiling to the `postgresql` dialect SQL string and checking the `ON CONFLICT (...) WHERE ... DO UPDATE SET ...` clause and the number of statements — **no DB**.

**Rationale.** Satisfies "bounded number of statements regardless of batch size" (SC-003) and idempotent re-push (SC-002) while keeping the tricky Postgres rules (one arbiter per insert, no double-affect) correct. Purity makes it fully unit-testable here.

**Alternatives rejected.** (a) Row-by-row `select-or-insert` — violates Principle VIII/SC-003. (b) `ON CONFLICT DO NOTHING` + separate UPDATE — two passes, still needs dedup, and loses "update in place" semantics. (c) `INSERT ... ON CONFLICT` with a single combined arbiter across identity kinds — impossible (one arbiter). (d) First-wins dedup — spec mandates **last-wins** (§24, FR-012, edge case).

---

## D5 — Default-variant derivation (pure)

**Decision.** `app_shared/catalog/default_variant.py` exposes a pure `derive_default_variant(product) -> dict` used by both the create endpoint and bulk upsert. Rules: the default variant's `title` = the product's title if present else the literal `"Default"`; it **inherits** `sku`, `url` from the product when provided; `current_price`/`currency` are inherited from the product-create payload's optional price/currency fields **if supplied** (a "simple product" create may carry a price), else left null/caller-required per schema. `option_values` is null. `status = active`. A companion `ensure_at_least_one(variants)` returns the given variants unchanged if non-empty, else `[derive_default_variant(product)]`.

The literal default title is `"Default"` (not the product title) **only when the product has no title** — a product always has a `title` (§22 required), so in practice the default variant's title equals the product title; `"Default"` is the documented fallback for the degenerate/edge path and for a title-less bulk row.

**Rationale.** Constitution III mandates every product (incl. simple) has ≥1 default variant carrying variant-level price; §35-04 leaves the exact title a plan choice — inheriting the product title (fallback `"Default"`) is the least surprising and keeps the `unique(workspace_id, product_id, title)` rule satisfiable (one default per product). Purity → unit-testable without a DB.

**Alternatives rejected.** (a) Always title `"Default"` — loses the human-readable product name on the sole variant. (b) Product-level price column to avoid a default variant — **forbidden** by Principle III (no product-level price). (c) Lazily creating the default only at first price write — leaves a window where a product has zero variants, violating FR-006.

---

## D6 — Cursor pagination (opaque keyset)

**Decision.** `app_shared/pagination.py` (framework-agnostic): `encode_cursor(created_at, id) -> str` = `base64url(json({"c": created_at.isoformat(), "id": str(id)}))`; `decode_cursor(str) -> (datetime, uuid)` validating shape and raising a typed `InvalidCursor` on garbage. Ordering key is `(created_at, id)` (id breaks ties deterministically; UUIDv7 is time-ordered so this is near-natural order). A `keyset_predicate(model, after)` helper builds the SQLAlchemy `(model.created_at, model.id) > (c, id)` tuple comparison for the seek. `LIMIT` handling: `clamp_limit(requested) -> min(requested or 50, 500)` (default 50, hard cap 500). List endpoints fetch `limit+1` rows to compute `next_cursor` (null when no more). Opaque = clients treat it as a token, never parse it.

**Rationale.** Keyset beats OFFSET at scale (§39, Principle VIII) — no growing table scan. `(created_at, id)` is stable and total-order. Opaque base64(json) is trivially unit-testable (round-trip + malformed rejection + clamp) with no DB.

**Alternatives rejected.** (a) OFFSET/LIMIT — O(n) skip, unsafe at 2k+ rows per list, and unstable under concurrent inserts. (b) Raw `id`-only cursor — UUIDv7 is time-ordered so `id` alone nearly works, but `(created_at, id)` is explicit and survives any future non-UUIDv7 id. (c) Encrypted cursor — unnecessary; the tuple leaks nothing sensitive, and RLS/scoping already bound visibility.

---

## D7 — Schemas location (apps/api, not app_shared)

**Decision.** Pydantic v2 request/response DTOs live in `apps/api/app/schemas/catalog.py`. `app_shared` stays FastAPI/Pydantic-**API**-free; only framework-agnostic domain logic (models, pagination helper, upsert/default-variant/consistency core, which operate on plain dicts / SQLAlchemy) lives in `app_shared`. The master §5 tree lists an `app_shared/schemas/` slot, but per the plan-level guidance we keep API request/response schemas in `apps/api` to preserve the import boundary (the `test_import_boundaries.py` guard forbids FastAPI in `app_shared`); Pydantic itself is not FastAPI, but the request/response *contract* schemas are an API-layer concern and are only consumed by routers.

**Rationale.** Preserves the one-way dependency and the FastAPI-free `app_shared` invariant (Principle I). The pure core in `app_shared` takes/returns plain dicts so both the routers and the bulk-upsert path share it without importing API schemas.

**Alternatives rejected.** (a) Schemas in `app_shared/schemas` — risks pulling API concerns into the shared lib and blurs the boundary the import-guard protects. (b) Dataclasses instead of Pydantic in the router layer — loses FastAPI's validation/OpenAPI integration for no benefit.

---

## D8 — Migration head, RLS on all four, single-head

**Decision.** One hand-authored Alembic revision (`<rev>_catalog_tables.py`) with `down_revision = "55da7d6d939d"` (verified current head via `alembic heads`). It `create_table`s all four (exact §22 shapes, `String(length=32)` status columns to match `enum_column`, `Money`→`sa.Numeric(18,4)`, `currency` `CHAR(3)`, `option_values` `JSONB`), creates the partial unique indexes (`op.create_index(..., unique=True, postgresql_where=sa.text("... IS NOT NULL"))`), the `unique(workspace_id, id)` parents and composite FKs, then calls `emit_rls_policy(t)` for **all four** tables in the **same** migration (Principle II / §32). `downgrade()` drops in FK-safe order (group_items → groups, variants → products). Hand-authored (not autogenerated) — no live Postgres in this env — reproducing `app_shared.models.catalog` exactly under the SPEC-02 `NAMING_CONVENTION`. Validated by `alembic upgrade head --sql` offline render + the existing `scripts/check_single_head.sh`.

**Rationale.** Matches the SPEC-03 migration precedent exactly (hand-authored, RLS in-migration, single linear head). RLS on all four is non-negotiable (all four are workspace-owned).

**Alternatives rejected.** (a) Autogenerate — no DB to reflect against; also wouldn't emit RLS or partial-index `WHERE` cleanly. (b) Separate migration for RLS — violates "RLS in the creating migration" (§32). (c) Branching head — breaks the single-head CI guard.

---

## D9 — Repository `ModelT` bound + CI guard coverage

**Decision.** `repository.py` currently has a **constrained** `TypeVar("ModelT", User, ApiKey)`; widen it to `TypeVar("ModelT", bound=Base)` so `scoped_select`/`scoped_get` type-check for the four new models too (runtime behavior identical — the helpers already work for any model with `.id`/`.workspace_id`). Add `Product, ProductVariant, ProductGroup, ProductGroupItem` to `WORKSPACE_OWNED_MODELS`. Because the AST guard (`scripts/check_workspace_scoping.py`) imports that exact frozenset, adding the four there is **sufficient** for the guard to cover them — no guard code change. Verified: guard still passes on the current tree with the four added (no unscoped `select(Product)`/`get(...)` exists), and a planted violation is flagged (new `test_catalog_scoping_guard.py`).

**Rationale.** Single source of truth for "workspace-owned" (the frozenset) already wires the guard; extending it is the whole job. The `bound=Base` widening removes the friction of a constrained TypeVar without weakening the runtime scoping assertions.

**Alternatives rejected.** (a) Leave the constrained TypeVar and add four members — brittle and unbounded growth; `bound=Base` is cleaner. (b) A second frozenset for catalog — would let the guarded set drift from the runtime set (the exact failure the single-set design prevents).

---

## D10 — Unit-testable-here vs live-DB split

**Decision.** Fully unit-tested in this env (no DB): model/column/constraint/index **shapes** + naming render; partial-index `postgresql_where` render; RLS DDL render for the four tables; default-variant derivation + ≥1-variant invariant; bulk-upsert statement construction (compile to SQL) + identity ordering + last-wins dedup + statement-count bound; cursor encode/decode round-trip + malformed rejection + limit clamp; workspace-consistency check logic; scope-gating wiring (routers declare correct `require_scopes`); the CI scoping guard passing with the four models + flagging a planted violation; migration **offline** `--sql` render + single head; import-boundary (no fastapi/scrapy in `app_shared`).

Authored + **marked for a Postgres host** (skipped here): actual create → default-variant persistence; bulk upsert 0-dupe / in-place update / last-wins on real data; RLS row denial + cross-workspace blocking + no-context-zero-rows; duplicate-membership rejection; delete hard-vs-archive outcome; migration **online** run; end-to-end request flows (scope refusal 403, read/write success).

**Rationale.** Mirrors the SPEC-03 split (no Docker/live Postgres here). Compile-to-SQL makes even the upsert path assertable without execution.
