# Autospec Decisions — SPEC-04 Catalog: Products, Variants, Groups

Feature directory: `specs/004-catalog-products-variants-groups`
Master doc: `/srv/crawmatic/PROJECT_SPEC.md`

## specify

- [specify] Q: Any clarifications needed? → A: No NEEDS CLARIFICATION markers; requirements fully specified by the doc (source: §22 Catalog tables + unique constraints + deletion semantics, §24 endpoints + pagination + bulk-upsert rules, §19 money, §32 isolation, §35 subsection "04").
- [specify] Q: Feature short-name / directory? → A: `specs/004-catalog-products-variants-groups` (sequential; matches doc §5 dir).
- [specify] Q: Scope? → A: 4 catalog tables (products, product_variants, product_groups, product_group_items) + their CRUD/bulk-upsert/grouping endpoints + default-variant behavior + workspace isolation. NO competitors/matches (05), scrape-profiles (06), scraping/prices/alerts (07+) (source: §35 04 vs 05/06/07).
- [specify] Q: Default variant behavior? → A: simple product (no explicit variants) auto-gets exactly one default variant carrying product price/currency/sku/url; product with explicit variants gets no spurious default; product always retains ≥1 variant (source: doc §35 04 + Constitution III variant-level pricing).
- [specify] Q: Bulk upsert semantics? → A: set-based INSERT...ON CONFLICT (bounded statements, no row loops); identity resolution external_id → sku → (product_id,title); in-batch dup = last-wins deterministic (source: §24).
- [specify] Q: Unique constraints? → A: partial uniques on (workspace_id, external_id) / (workspace_id, sku) where present; variants also (workspace_id, product_id, title); groups (workspace_id, name); group_items (workspace_id, group, product) + (workspace_id, group, variant) (source: §22 — SPEC-02 naming convention disambiguates shared-first-column pairs).
- [specify] Q: Deletion semantics? → A: archive-by-status once dependent history exists; hard-delete allowed now (no history tables until 07+) but structured for archive; response indicates which (source: §22/§24).
- [specify] Q: Scope-gating? → A: FIRST spec to exercise end-to-end scope enforcement — products:read/write, variants:read/write via SPEC-03 require_scopes; group items reuse products/variants write scopes (no dedicated group scope in vocab) (source: §24/§33 scope vocab).
- [specify] Q: FK workspace-consistency? → A: all FKs workspace-local; reference to another workspace's entity rejected in application layer (and/or composite FK incl workspace_id) (source: §32).
- [specify] Q: Live-Postgres acceptance given no daemon here? → A: DB-independent logic unit-tested here (model/naming render, default-variant derivation, bulk-upsert statement construction, cursor encode/decode, scope-gating, RLS render, workspace-consistency checks); live items (create/upsert, RLS row denial, cross-workspace, migration run, e2e flows) deferred to a PG host (source: no-docker-daemon constraint).

## clarify

## plan (opus subagent)

- [plan] Models → app_shared/models/catalog.py (Product/ProductVariant/ProductGroup/ProductGroupItem on WorkspaceScopedBase; group-item created_at-only). Enums ProductStatus/VariantStatus/GroupStatus (active/archived) in enums.py; no GroupItemStatus (§22 has none). Added all 4 to WORKSPACE_OWNED_MODELS (guard covers them).
- [plan] Partial uniques + ON CONFLICT → partial unique Index(postgresql_where=... IS NOT NULL); bulk upsert targets each via pg_insert.on_conflict_do_update(index_elements, index_where matching predicate). Batch partitioned by identity kind → ≤3 statements/table (still set-based, VIII).
- [plan] Default variant → pure derive_default_variant (title=product title, fallback "Default"; inherits sku/url/price/currency) + ensure_at_least_one.
- [plan] Bulk upsert → set-based, identity order external_id→sku→(product_id,title), in-batch last-wins dedup, bounded statements, default-variant for zero-variant products; compile-to-SQL unit-testable.
- [plan] Cursor pagination → opaque base64(json) keyset over (created_at,id), default 50/cap 500, app_shared/pagination.py (framework-agnostic).
- [plan] FK workspace-consistency → composite FKs (workspace_id, ref_id)→parent(workspace_id, id) (parents get unique(workspace_id, id)) — structural — plus pure app pre-check for clean 404/422.
- [plan] Schemas → apps/api/app/schemas/catalog.py (keeps app_shared FastAPI-free); pure core takes/returns dicts.
- [plan] Migration → one revision, down_revision=55da7d6d939d (current head), RLS on all 4 tables in creating migration, single head; offline render.
- [plan] Constitution Check → PASS (all 8; II/III/VII/VIII satisfied). Artifacts: plan.md, research.md (D1-D10), data-model.md, quickstart.md, contracts/{api-products,api-variants,api-product-groups,models-catalog,catalog-bulk-upsert,default-variant,pagination,workspace-consistency,migration-catalog}.md.

## clarify

Run doc-first INLINE (context conservation — identical no-op doc-first pattern as SPEC-01/02/03; SPEC-04 doc coverage equally complete). No questions to user. Doc-resolved clarifications recorded in spec.md `## Clarifications` (Session 2026-07-03): workspace-owned/RLS on all 4 tables; default-variant behavior (title=plan-level); set-based bulk upsert + identity order + last-wins (partial-unique ON CONFLICT=plan-level); NUMERIC(18,4)+3-letter currency; archive-by-status deletion; cursor pagination 50/500 (encoding=plan-level); scope-gating products/variants read/write (group items reuse write scopes); FK workspace-consistency in app layer; live items deferred. Requirements checklist re-validated: 16/16 still pass.
