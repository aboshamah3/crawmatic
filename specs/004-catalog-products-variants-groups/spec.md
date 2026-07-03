# Feature Specification: Catalog — Products, Variants & Groups

**Feature Branch**: `004-catalog-products-variants-groups`

**Created**: 2026-07-03

**Status**: Draft

**Input**: SPEC-04 from PROJECT_SPEC.md §35 — store the client's catalog (products, variants, groups) with correct variant-level structure, bulk ingestion, and workspace isolation.

## Clarifications

### Session 2026-07-03

All items below were resolved from the master specification (`PROJECT_SPEC.md` §19/§22/§24/§32) and the SPEC-02/03 foundation; no open ambiguity required a stakeholder decision. Plan-level details are noted as such.

- Q: Which tables are workspace-owned / get RLS now? → A: All four (products, product_variants, product_groups, product_group_items) — each carries `workspace_id`, gets RLS enabled+forced in its creating migration (SPEC-02 `emit_rls_policy`), and is added to `WORKSPACE_OWNED_MODELS` (source: §22/§32).
- Q: Default-variant behavior and title? → A: A product with no explicit variants auto-gets exactly one default variant inheriting its price/currency/sku/url; a product with explicit variants gets none added; product always keeps ≥1 variant. The default variant's title when none is derivable (e.g. `"Default"` vs the product title) is a plan-level choice (source: §35 04 + Constitution III).
- Q: Bulk-upsert mechanism and in-batch conflicts? → A: Set-based `INSERT … ON CONFLICT` (bounded statements, no row loops); identity resolution `external_id` → `sku` → `(product_id, title)`; in-batch duplicates resolve last-wins. The exact ON CONFLICT target handling for the *partial* unique indexes (where external_id/sku is null) is a plan-level detail (source: §24).
- Q: Money & currency? → A: finite `NUMERIC(18,4)` via the SPEC-02 Money type; `currency` is a 3-letter code; no cross-currency comparison in v1 (source: §19).
- Q: Deletion semantics now vs later? → A: hard-delete allowed now (no dependent history tables until SPEC-07+) but the model/endpoint are structured for archive-by-status, and the delete response indicates which outcome occurred (source: §22/§24).
- Q: Pagination? → A: cursor-based, default 50, max 500, `{items, next_cursor}`; the opaque cursor encoding (e.g. of `(created_at, id)`) is a plan-level detail (source: §24).
- Q: Scope-gating (first end-to-end use)? → A: products:read/write and variants:read/write via the SPEC-03 `require_scopes` auth seam; product-group management reuses the products/variants write scopes (no dedicated group scope exists in the §33 vocabulary) (source: §24/§33).
- Q: FK workspace-consistency? → A: all foreign references (variant→product, group_item→product/variant/group) must resolve within the same workspace; cross-workspace/nonexistent references are rejected in the application layer (composite FK including workspace_id where practical) (source: §32).
- Q: Live-Postgres acceptance items here? → A: DB-independent logic unit-tested here; live create/upsert, RLS row denial, cross-workspace blocking, migration run, and end-to-end request flows authored + deferred to a Postgres host (source: no-docker-daemon constraint).

## User Scenarios & Testing *(mandatory)*

The users of this feature are workspace operators and their integrations (WooCommerce/Salla-style connectors) that load and manage a client's product catalog. This feature is the first to store real business data: products, their variants, and groupings — each confined to one workspace, each priceable at the variant level, and ingestible in bulk. It is also the first feature to exercise end-to-end scope-gated API access on top of the auth foundation.

### User Story 1 - Create and manage products and variants (Priority: P1)

An operator creates a product; a product with no explicit variants automatically gets a single default variant so it can be priced and matched at the variant level; the operator can read, update, list, and (when safe) delete products, and read/update variants.

**Why this priority**: The catalog is the root object everything downstream (competitors, matches, prices) attaches to; without it nothing else can exist.

**Independent Test**: Create a simple product → confirm exactly one default variant is created carrying the product's price/currency/sku/url; create a product with explicit variants → those variants exist and no spurious default is added; read/update/list products and variants and confirm changes persist within the workspace.

**Acceptance Scenarios**:

1. **Given** a valid product payload with no variants, **When** the product is created, **Then** it is stored and a single default variant is automatically created for it (so every product has at least one variant).
2. **Given** a valid product payload with one or more explicit variants, **When** it is created, **Then** the product and its variants are stored and no extra default variant is added.
3. **Given** an existing product, **When** it is read, updated (PATCH), or listed, **Then** the operation reflects the current workspace-scoped state.
4. **Given** an existing variant, **When** it is read or updated, **Then** the change persists and monetary fields remain valid finite decimals with a currency code.
5. **Given** a product with no dependent history, **When** it is deleted, **Then** it is hard-deleted and the response indicates a hard delete; **Given** a product that later has dependent history (a future capability), the model and endpoint are structured to archive it by status instead, and the response indicates which occurred.

### User Story 2 - Bulk-upsert a Woo/Salla-style catalog payload (Priority: P1)

An integration pushes a batch of products and variants in one call; the system upserts them set-based (not row-by-row), resolving each record's identity by external id, then SKU, then (for variants) product + title — so re-pushing an updated catalog updates existing rows instead of duplicating them.

**Why this priority**: Real catalogs arrive in bulk from external stores; a correct, idempotent, set-based upsert is the primary ingestion path and a scale-safety requirement.

**Independent Test**: Bulk-upsert a payload of several products+variants → all are created; re-push the same payload with changes → existing rows are updated (matched by external id → SKU → product+title) and no duplicates are created; the operation is set-based regardless of batch size.

**Acceptance Scenarios**:

1. **Given** a batch of new products and variants, **When** bulk-upsert runs, **Then** all records are inserted and each product still ends up with at least one variant (default applied where none given).
2. **Given** a batch whose records match existing rows by external id (or SKU, or product+title for variants), **When** bulk-upsert runs again, **Then** matching rows are updated in place and no duplicates are created.
3. **Given** a large batch, **When** bulk-upsert runs, **Then** it executes as a set-based operation (a bounded number of statements), not one statement per record.
4. **Given** two records in the same batch that resolve to the same identity, **When** bulk-upsert runs, **Then** the conflict is resolved deterministically (last-wins or an explicit rule) without error.

### User Story 3 - Group products and variants (Priority: P2)

An operator organizes products and/or variants into named groups (e.g. a category or a monitoring set) and adds/removes items, so downstream monitoring and reporting can target a group.

**Why this priority**: Grouping adds organizational value but builds on products/variants existing first.

**Independent Test**: Create a group; add a product and a variant to it; list groups; remove an item; confirm the group and its membership are workspace-scoped and that duplicate memberships are rejected.

**Acceptance Scenarios**:

1. **Given** a workspace, **When** a group is created and listed, **Then** it appears with its name/description/status, scoped to the workspace.
2. **Given** a group, **When** a product or a variant is added as an item, **Then** the membership is recorded once; re-adding the same product/variant to the same group is rejected (no duplicate membership).
3. **Given** a group item, **When** it is removed, **Then** the membership no longer exists.
4. **Given** a group and its items, **When** they reference products/variants, **Then** all referenced entities belong to the same workspace as the group.

### User Story 4 - Catalog access is workspace-isolated and scope-gated (Priority: P1)

Every catalog operation runs under one workspace context and requires the appropriate capability (read vs. write); a caller cannot read or write another workspace's catalog, and a credential lacking the needed capability is refused.

**Why this priority**: Isolation is non-negotiable, and this is the first feature to prove end-to-end scope enforcement on real business endpoints.

**Independent Test**: With two workspaces populated, confirm a workspace-A caller cannot read or write workspace-B products/variants/groups (including when an application filter is omitted — row-level security blocks); confirm a read-scoped credential cannot perform writes and a write-scoped credential can.

**Acceptance Scenarios**:

1. **Given** a caller authenticated to workspace A, **When** it requests workspace B's product/variant/group by id or in a list, **Then** it receives none of B's data.
2. **Given** a query that omits the workspace filter, **When** it runs against a catalog table, **Then** row-level security still returns zero rows from other workspaces (defense-in-depth), and zero rows when no workspace context is set.
3. **Given** a credential with only read capability for a resource, **When** it attempts a write, **Then** the write is refused; **Given** a credential with the write capability, **When** it writes, **Then** the write succeeds.
4. **Given** the codebase, **When** the continuous-integration scoping guard runs, **Then** it fails if any unscoped query on a catalog (workspace-owned) model is introduced.

### Edge Cases

- What happens when a product is created with neither external id nor SKU? It is still stored (both are optional) and gets a default variant; identity for future upserts falls back to title-based resolution where applicable.
- What happens when a variant references a product in a different workspace (or a nonexistent product)? The operation is rejected — foreign references must resolve within the same workspace.
- What happens when a group item references a product/variant in a different workspace? Rejected — membership references must be workspace-local.
- What happens when the same product/variant is added to a group twice? The duplicate membership is rejected by the uniqueness rule.
- What happens when a bulk-upsert batch contains two records with the same external id (or SKU)? The in-batch conflict is resolved deterministically (last-wins) rather than erroring.
- What happens when a bulk-upserted product has neither external id nor SKU? It has no identity key, so it is always inserted (never matched to an existing row) — re-pushing such a product creates another row. This is a documented limitation: callers who need identity-less products to upsert must supply a client key (external id or SKU).
- What happens when a bulk payload nests variants under products referenced by external id/SKU (Woo/Salla style)? Each product's identity is resolved first, then its variants resolve against that product (by external id → SKU → the product + title), so nested variants attach to the correct upserted product.
- What happens when a monetary value is non-finite or over-precise, or a currency code is malformed? It is rejected at the boundary (finite `NUMERIC(18,4)`, 3-letter currency) rather than stored.
- What happens when a product with variants is deleted while (in a future spec) it has dependent history? It is archived by status rather than hard-deleted; the response distinguishes the two outcomes.
- What happens when a list request exceeds the maximum page size? The page size is capped at the maximum and results are paginated by cursor.
- What happens on a "simple" product update that would leave it with zero variants? The product must always retain at least one variant.

## Requirements *(mandatory)*

### Functional Requirements

**Catalog data & isolation**
- **FR-001**: The system MUST provide four workspace-owned catalog entities — products, product variants, product groups, and product group items — each carrying `workspace_id` and each protected by row-level security enabled in the same migration that creates it.
- **FR-002**: All four catalog models MUST be registered as workspace-owned so the workspace-scoped repository helpers and the continuous-integration unscoped-query guard cover them; every catalog query MUST be workspace-scoped (or fail the guard).
- **FR-003**: Cross-workspace reads and writes of catalog data MUST be blocked both by application scoping AND by row-level security (fail closed when no workspace context is set), proven by automated tests.

**Products & variants**
- **FR-004**: A product MUST support optional `external_id`, `sku`, `brand`, `barcode`, `url`, a `title`, and a `status`; a variant MUST support optional `external_id`, `sku`, `barcode`, `url`, a `title`, `option_values`, a finite `NUMERIC(18,4)` `current_price`, a 3-letter `currency`, a required parent product, and a `status`.
- **FR-005**: Creating a product with no explicit variants MUST automatically create exactly one default variant for it (carrying the product's price/currency/sku/url as applicable), so every product has at least one variant; a product provided with explicit variants MUST NOT receive an extra default variant.
- **FR-006**: A product MUST always retain at least one variant; an update that would remove a product's last variant MUST be prevented (or re-establish a default).
- **FR-007**: Monetary values MUST be validated as finite decimals within `NUMERIC(18,4)` scale and rejected otherwise; currency MUST be a 3-letter code; no cross-currency comparison is performed in this feature.
- **FR-008**: The system MUST enforce the uniqueness rules: products unique on `(workspace_id, external_id)` and `(workspace_id, sku)` (each only where the value is present); variants unique on `(workspace_id, external_id)`, `(workspace_id, sku)` (each where present) and `(workspace_id, product_id, title)`; product groups unique on `(workspace_id, name)`; group items unique on `(workspace_id, product_group_id, product_id)` and `(workspace_id, product_group_id, product_variant_id)`.
- **FR-009**: All foreign references (a variant's product, a group item's product/variant/group) MUST resolve within the same workspace; a reference to another workspace's entity or a nonexistent entity MUST be rejected.

**Bulk upsert**
- **FR-010**: The system MUST provide set-based bulk upsert for products and for variants that inserts-or-updates in a bounded number of statements (never one statement per record), regardless of batch size.
- **FR-011**: Bulk-upsert identity resolution MUST follow the order: `external_id`, then `sku`, then (for variants) `(product_id, title)`; matched records update in place, unmatched records insert, and no duplicates are created on re-push. A **product** supplied with neither `external_id` nor `sku` has no stable identity key and MUST be treated as always-insert (it cannot be deduplicated on re-push); this limitation MUST be documented so callers know identity-less products require a client-supplied key to be upsertable.
- **FR-012**: Bulk upsert MUST resolve two in-batch records that map to the same identity deterministically (last-wins) without error, and MUST still guarantee each upserted product ends with at least one variant.

**Grouping**
- **FR-013**: The system MUST allow creating, listing, updating, and deleting product groups, and adding/removing group items that reference a product or a variant; duplicate membership of the same product/variant in the same group MUST be rejected.

**Endpoints, pagination, deletion**
- **FR-014**: The system MUST expose the catalog endpoints under `/v1` — products (create, list, get, update, delete, bulk-upsert), variants (list, get, update, bulk-upsert), and product groups (create, list, update, delete, add item, remove item) — and nothing outside catalog scope.
- **FR-015**: List endpoints MUST use cursor-based pagination with a default page size of 50 and a maximum of 500, returning `{items, next_cursor}`.
- **FR-016**: Every catalog endpoint MUST run under the request's workspace context and MUST be gated by the appropriate capability (read vs. write per resource family); a credential lacking the required capability MUST be refused, and one holding it MUST be allowed.
- **FR-017**: Deletion of a product or group MUST hard-delete only when no dependent history exists and otherwise archive by status; because no history exists yet in this feature, deletes may hard-delete now, but the model and endpoint MUST be structured for archive-by-status and the response MUST indicate which outcome occurred.

### Key Entities *(include if data involved)*

- **Product**: A workspace-owned catalog item (optional external id/SKU/brand/barcode/url, title, status). Always has at least one variant.
- **Product variant**: A workspace-owned, product-scoped priceable unit (optional external id/SKU/barcode/url, title, option values, finite money price + 3-letter currency, status). The unit at which pricing and (later) matching occur.
- **Product group**: A workspace-owned named collection (name unique per workspace, optional description, status).
- **Product group item**: A workspace-owned membership linking a group to a product or a variant, unique per (group, product) and (group, variant).
- **Bulk-upsert batch**: A set of product/variant records ingested in one set-based operation with deterministic identity resolution.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Creating a simple product yields exactly one variant (the auto default) 100% of the time; creating a product with N explicit variants yields exactly N variants (0 spurious defaults).
- **SC-002**: Re-pushing an unchanged bulk payload creates 0 duplicate products/variants; re-pushing a changed payload updates the matched rows in place (matched by external id → SKU → product+title).
- **SC-003**: A bulk upsert of any batch size executes in a bounded number of statements (not proportional to record count) — 0 row-by-row insert loops.
- **SC-004**: In a two-workspace test, 0 rows of workspace B's catalog are read or written by a workspace-A caller, including when the application filter is omitted (row-level security blocks) and when no workspace context is set (fail closed, 0 rows).
- **SC-005**: A read-capability-only credential completes 0 successful writes; a write-capability credential completes writes successfully; 100% enforcement.
- **SC-006**: The continuous-integration scoping guard fails the build on 100% of introduced unscoped queries against catalog models.
- **SC-007**: 100% of stored monetary values are finite and within scale with a 3-letter currency; non-finite/over-precise/malformed values are rejected at the boundary.
- **SC-008**: The uniqueness rules hold: 0 duplicate `(workspace_id, external_id)` / `(workspace_id, sku)` products or variants, 0 duplicate group names per workspace, 0 duplicate group memberships.
- **SC-009**: List endpoints never return more than 500 items per page and always provide a cursor to fetch the next page when more results exist.

## Assumptions

- This feature builds on the SPEC-02 database foundation (UUIDv7 ids, TZ timestamps, all-columns naming convention, finite `NUMERIC(18,4)` money type, string-backed enums, the row-level-security policy helper) and the SPEC-03 isolation machinery (workspace-scoped base/repository helpers, the per-request workspace context that sets the isolation key inside the transaction, the API-key scope vocabulary including `products:read/write` and `variants:read/write`, and the continuous-integration unscoped-query guard).
- Product-group items reuse the `products`/`variants` write scopes for management (no dedicated group scope in the vocabulary); this is the reasonable mapping given the defined scope set.
- "Simple product" means a product supplied without explicit variants; the auto-created default variant inherits the product's price/currency/SKU/URL where provided and a default title otherwise.
- Cross-currency comparison and price analysis are out of scope (later specs); this feature only stores validated monetary values.
- Competitors, matches, scrape profiles, observations, prices, and alerts are out of scope (SPEC-05+). The catalog tables carry no references to those yet.
- Build/CI environment has no live PostgreSQL (no container engine here): DB-independent logic (model/constraint shapes and naming render, default-variant derivation, bulk-upsert statement construction and identity-resolution ordering, pagination cursor encode/decode, scope-gating wiring, row-level-security DDL render, workspace-consistency checks) is fully unit-tested here; acceptance items requiring a live database (actual create/upsert, row-level-security row denial, cross-workspace blocking, migration run, end-to-end request flows) are authored and validated on a PostgreSQL-capable host.
