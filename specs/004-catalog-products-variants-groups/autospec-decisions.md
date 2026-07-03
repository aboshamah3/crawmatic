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

## analyze (inline, forked)

0 CRITICAL/HIGH → no user pause. Remediated all 3 findings:
- [analyze] F2 (MEDIUM): FR-006 last-variant invariant had no triggering operation (no variant DELETE; PATCH doesn't touch variant count) → A: reworded FR-006 as a structural service-layer guard (ensure_at_least_one, unit-tested); removed the untestable zero-variant 409 path from T018.
- [analyze] F1 (LOW): FR-014 omitted GET product-groups/{id} (tasks/plan had it) → A: added "get" to the groups enumeration.
- [analyze] F3 (LOW): T011 lacked the {items,next_cursor} envelope more/none-branch assertion (SC-009) → A: added envelope-builder assertion (limit+1 rows → cursor set; ≤limit → null).
- Only MEDIUM/LOW (no CRITICAL/HIGH); clarification-only changes → full re-run not required. 100% FR/SC coverage retained.

## implement (sonnet subagents, 4 groups)

33/39 tasks [X]; 6 DEFERRED (live PG). Suite: 335 passed, 45 skipped.
- A Setup+Foundational (T001-T013): 4 catalog models (partial unique indexes, composite workspace-local FKs, unique(workspace_id,id) parents), migration c2987b29555e w/ RLS on all 4, pagination.py, WORKSPACE_OWNED_MODELS +4. Constraint names shortened to fit 63-byte limit.
- B US1 (T014-T019): default_variant core, schemas (money/currency validators), products CRUD + variants list/get/patch routers (scope-gated, keyset pagination, hard-delete outcome).
- C US2 (T021-T027): consistency.py, upsert.py (pure ≤3-stmt/table set-based ON CONFLICT matching partial indexes, last-wins, default-variant injection, identity-less always-insert), bulk endpoints.
- D US4+US3+Polish (T029-T030,T032-T034,T036-T037): scope-gating + guard tests, product_groups router (CRUD+items, add-variant needs variants:write, dup->409, consistency pre-check), identity-less docs, validation gate.

### DEFERRED — live Postgres (authored + skip cleanly; not gaps)
T020 products CRUD, T028 bulk idempotency, T031 workspace isolation/RLS denial, T035 groups membership, T038 online migration, T039 live quickstart e2e. Cover live halves of SC-001/002/004/005/008 + online migration.

## converge (opus subagent)

- Result: CONVERGED — no new tasks (tasks.md unchanged). Static sweep all PASS:
  guard exit 0 + catches planted unscoped select(Product); one head c2987b29555e; offline render = 4 catalog tables + 6 partial unique indexes (WHERE ... IS NOT NULL) + 12 catalog RLS statements (ENABLE+FORCE+NULLIF); bulk-upsert ON CONFLICT index_where matches model partial-index predicates exactly; no unannotated unscoped catalog access; app_shared/catalog framework-agnostic; default-variant + money/currency + scope-gating (add-variant needs variants:write) unit-verified; Base.metadata = _smoke_foundation + 4 identity + 4 catalog (9 tables, no SPEC-05 leak); 17 catalog routes under /v1.
- FR-001..FR-017 built/verified here; SC buildable portions verified; 6 daemon-deferred live-PG items are the only open verifications. Converged cycle 1, no implement re-loop.

## checklist

Run substance INLINE (context conservation). Generated checklists/catalog.md (29 requirements-quality items). Gap found + fixed before checking: bulk-upsert identity for a product with NO external_id/sku was unspecified (would silently duplicate on re-push) → added to FR-011 + 2 edge cases (always-insert documented limitation; nested Woo/Salla product+variant resolution order). Completion: catalog.md 29/29 pass; requirements.md 16/16 pass. Implement gate CLEAR.

## clarify

Run doc-first INLINE (context conservation — identical no-op doc-first pattern as SPEC-01/02/03; SPEC-04 doc coverage equally complete). No questions to user. Doc-resolved clarifications recorded in spec.md `## Clarifications` (Session 2026-07-03): workspace-owned/RLS on all 4 tables; default-variant behavior (title=plan-level); set-based bulk upsert + identity order + last-wins (partial-unique ON CONFLICT=plan-level); NUMERIC(18,4)+3-letter currency; archive-by-status deletion; cursor pagination 50/500 (encoding=plan-level); scope-gating products/variants read/write (group items reuse write scopes); FK workspace-consistency in app layer; live items deferred. Requirements checklist re-validated: 16/16 still pass.
