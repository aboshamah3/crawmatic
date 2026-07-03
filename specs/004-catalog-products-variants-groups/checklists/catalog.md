# Catalog Data-Integrity & Isolation Requirements Quality Checklist: Catalog

**Purpose**: Validate that SPEC-04's requirements for catalog structure, default-variant behavior, bulk upsert, uniqueness/isolation, and scope-gated access are complete, clear, consistent, and measurable before implementation.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [contracts/](../contracts/)
**Depth**: Standard · **Audience**: Reviewer (pre-implementation gate)

## Requirement Completeness

- [x] CHK001 - Are all four catalog entities (products, variants, groups, group items) and their attributes fully specified per §22? [Completeness, Spec §FR-001, §FR-004]
- [x] CHK002 - Is the default-variant behavior fully specified (auto-create exactly one when none given; none added when explicit variants exist; always ≥1 variant)? [Completeness, Spec §FR-005, §FR-006]
- [x] CHK003 - Are the uniqueness rules fully enumerated for all four tables, including the partial (where-present) uniques and the shared-first-column pairs? [Completeness, Spec §FR-008]
- [x] CHK004 - Is set-based bulk upsert specified (bounded statements, no row-by-row loop) with identity-resolution order and in-batch last-wins? [Completeness, Spec §FR-010, §FR-011, §FR-012]
- [x] CHK005 - Are the catalog endpoints, pagination, and deletion semantics specified? [Completeness, Spec §FR-014, §FR-015, §FR-017]
- [x] CHK006 - Is workspace isolation on all four tables specified (RLS in the creating migration, added to the workspace-owned set, cross-workspace tests)? [Completeness, Spec §FR-001, §FR-002, §FR-003]
- [x] CHK007 - Is scope-gated access specified for read vs. write per resource family (first end-to-end scope enforcement)? [Completeness, Spec §FR-016]

## Requirement Clarity

- [x] CHK008 - Is "simple product" defined precisely enough to determine when a default variant is created? [Clarity, Spec §FR-005, Assumptions]
- [x] CHK009 - Is the bulk-upsert identity-resolution order unambiguous, including the no-identity-key product case (always-insert, documented)? [Clarity, Spec §FR-011, Edge Cases]
- [x] CHK010 - Is "set-based / bounded number of statements" made concrete enough to be verifiable (not proportional to record count)? [Clarity/Measurability, Spec §FR-010, §SC-003]
- [x] CHK011 - Is monetary/currency validation precise (finite `NUMERIC(18,4)`, 3-letter currency, reject otherwise)? [Clarity, Spec §FR-007, §SC-007]
- [x] CHK012 - Is the archive-by-status vs. hard-delete rule and its response indication unambiguous for the current (no-history) state? [Clarity, Spec §FR-017]

## Requirement Consistency

- [x] CHK013 - Do spec, plan, and constitution agree that pricing is variant-level (default variant), never product-level (Principle III)? [Consistency, Spec §FR-005, constitution §III]
- [x] CHK014 - Are the isolation requirements consistent with the SPEC-03 machinery reused (scoped repo helpers, per-request context, CI guard, RLS helper)? [Consistency, Spec Assumptions, §FR-002]
- [x] CHK015 - Is the FK workspace-consistency rule consistent across variants→product and group-items→product/variant/group? [Consistency, Spec §FR-009]
- [x] CHK016 - Is the scope vocabulary consistent (products/variants read/write) and is the group-item→write-scope mapping stated? [Consistency, Spec §FR-016, Assumptions, Clarifications]

## Acceptance Criteria Quality

- [x] CHK017 - Are success criteria SC-001–SC-009 objectively measurable (exact-count default variants, 0 duplicates, bounded statements, 0 cross-workspace rows, 100% scope enforcement, page cap)? [Measurability, Spec §SC-001–SC-009]
- [x] CHK018 - Is the "0 duplicates on re-push" outcome tied to concrete identity keys? [Measurability, Spec §SC-002, §SC-008]

## Scenario & Edge Case Coverage

- [x] CHK019 - Is the cross-workspace FK reference case (variant/group-item pointing at another workspace's entity) covered and required to be rejected? [Coverage, Spec §FR-009, Edge Cases]
- [x] CHK020 - Is the duplicate-group-membership case covered (rejected by uniqueness)? [Coverage, Spec §FR-013, Edge Cases]
- [x] CHK021 - Is the in-batch duplicate-identity case covered (deterministic last-wins, no error)? [Coverage, Spec §FR-012, Edge Cases]
- [x] CHK022 - Is the identity-less-product bulk case covered (always-insert, documented limitation)? [Coverage, Spec §FR-011, Edge Cases]
- [x] CHK023 - Is the nested Woo/Salla product+variant payload resolution covered (product resolved first, then its variants)? [Coverage, Spec Edge Cases, §FR-011]
- [x] CHK024 - Is the "update must not leave a product with zero variants" case covered? [Coverage, Spec §FR-006, Edge Cases]
- [x] CHK025 - Is the over-limit page-size case covered (capped at 500, cursor provided)? [Coverage, Spec §FR-015, §SC-009, Edge Cases]
- [x] CHK026 - Is the invalid-money/currency case covered (rejected at boundary)? [Coverage, Spec §FR-007, Edge Cases]

## Scope Discipline & Dependencies

- [x] CHK027 - Is out-of-scope explicit (no competitors/matches/scrape-profiles/observations/prices/alerts; catalog carries no references to them yet)? [Boundary, Spec Assumptions, §FR-014]
- [x] CHK028 - Are the SPEC-02/03 dependencies documented (Money type, RLS helper, WorkspaceScopedBase, auth seam/scopes, scoped repo helpers, CI guard)? [Assumption, Spec Assumptions]
- [x] CHK029 - Are the live-Postgres deferrals documented (create/upsert, RLS denial, cross-workspace, migration run, e2e flows), with DB-independent logic verifiable now? [Assumption, Spec Assumptions]

## Notes

- 29 items evaluated against spec.md (as amended) + plan.md + 9 contracts. **29/29 pass.**
- One gap surfaced during evaluation and was fixed before checking: bulk-upsert identity for a product with neither external_id nor sku was unspecified (would silently duplicate on re-push) → added to §FR-011 + two Edge Cases (always-insert documented limitation; nested product+variant resolution order). CHK009/CHK022/CHK023 then pass.
- ≥80% traceability: every item cites a spec section, success criterion, or edge case.
- Live-DB verification (create/upsert, RLS denial, cross-workspace) deferred to a Postgres host; this checklist validates requirement *quality*.
