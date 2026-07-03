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
