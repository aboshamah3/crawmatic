# Requirements Quality Checklist: Retention, Rollups & Partition Maintenance

**Purpose**: Unit-tests-for-requirements — validate that the SPEC-15 requirements are complete, clear, consistent, and measurable before implementation. Focus areas (risk-ranked): retention/ordering correctness, concurrency & idempotency, monetary aggregation correctness, boundary/edge coverage, workspace isolation & operability.
**Created**: 2026-07-06
**Feature**: [spec.md](../spec.md)
**Depth**: Standard (release-gate) · **Audience**: Reviewer (pre-implementation)

## Data-Safety & Ordering Correctness

- [x] CHK001 - Is the verify-before-drop ordering guarantee stated as a hard precondition (a partition MUST NOT be dropped before its covering rollups are verified)? [Completeness, Spec §FR-016/SC-004]
- [x] CHK002 - Is "rollups verified complete for a partition" defined with objective, testable criteria rather than left vague? [Clarity, Spec §Clarifications/Assumptions]
- [x] CHK003 - Is the retention mechanism unambiguously constrained to partition DROP and explicitly prohibited from bulk row DELETE on the raw append-heavy tables? [Clarity, Spec §FR-015/SC-003]
- [x] CHK004 - Is the retention eligibility boundary specified deterministically at partition granularity (entire range older than cutoff), removing "off-by-one" ambiguity? [Measurability, Spec §FR-018]
- [x] CHK005 - Are requirements defined for a raw partition whose rollups can never be verified (retain + surface, never silently drop nor silently accumulate)? [Edge Case, Spec §Edge Cases/FR-016]
- [x] CHK006 - Is the exception carved out for tables with no rollup dependency (drop by age alone) stated so it cannot be read as bypassing the ordering guarantee for observations? [Consistency, Spec §FR-019]

## Concurrency, Idempotency & Self-Healing

- [x] CHK007 - Are idempotency requirements defined for each of the three jobs (re-run yields same end state, no duplicate partition/rollup, no double drop, no error)? [Completeness, Spec §FR-006/FR-010/FR-020/SC-002]
- [x] CHK008 - Are concurrent/overlapping-run safety requirements stated (retried or simultaneous passes cannot duplicate or double-drop)? [Coverage, Spec §Edge Cases/FR-020]
- [x] CHK009 - Is the self-healing requirement (create current-month partition if missing, not only next-month) explicitly specified so a gap cannot cause write failure? [Completeness, Spec §FR-005]
- [x] CHK010 - Is "already-dropped partition is not an error" specified so retention re-runs are safe? [Clarity, Spec §FR-020]

## Monetary & Aggregation Correctness

- [x] CHK011 - Are the rollup aggregate outputs (client price, min/avg/max competitor, alert type, comparable count, currency, identity, date) each enumerated as required fields? [Completeness, Spec §FR-009]
- [x] CHK012 - Is the currency-comparability rule specified (only comparable, same-currency prices enter min/avg/max and the comparable count)? [Clarity, Spec §FR-011]
- [x] CHK013 - Are monetary correctness constraints stated (exact Decimal/NUMERIC, finite values only, no floating-point/NaN/Infinity)? [Completeness, Spec §FR-012]
- [x] CHK014 - Is the zero-comparable-competitor case specified (row still produced, competitor aggregates empty, count zero)? [Edge Case, Spec §FR-013]
- [x] CHK015 - Is the rollup uniqueness/upsert grain unambiguous (exactly one row per workspace+variant+day, upsert on that key)? [Measurability, Spec §FR-010/SC-006]
- [x] CHK016 - Is the date-attribution rule for a raw observation specified (which calendar date/timezone a row rolls up under), so aggregation and coverage are consistent? [Clarity, Spec §Clarifications]

## Scope, Registry & Dependencies

- [x] CHK017 - Is the set of partitioned tables and each table's retention window explicitly enumerated (observations/attempts/webhooks 90d, alert events 1y, rollups 2y)? [Completeness, Spec §FR-001/FR-017]
- [x] CHK018 - Is the "registered-but-absent table" behavior specified (skip webhook_events until it exists, without failing the pass)? [Edge Case, Spec §FR-002]
- [x] CHK019 - Is it clear this feature does not create/alter the raw parent tables, and does create the deferred rollup table? [Consistency, Spec §FR-009a/Assumptions]
- [x] CHK020 - Are the source surfaces for rollup data documented as dependencies (client price/alert type from the current-price surface; competitor prices from raw observations)? [Dependency, Spec §Assumptions]

## Workspace Isolation & Operability

- [x] CHK021 - Are workspace-isolation requirements for the new rollup table specified (workspace-owned, RLS, scoped writes) alongside the system/elevated cross-workspace execution context? [Completeness, Spec §FR-014]
- [x] CHK022 - Are observability requirements defined (each job records created/rolled-up/dropped/skipped/absent for operator visibility)? [Completeness, Spec §FR-023]
- [x] CHK023 - Is optional vacuum/analyze constrained so it can never block or fail the core create/rollup/drop guarantees? [Clarity, Spec §FR-024]
- [x] CHK024 - Are timezone semantics for all bounds/cutoffs/dates specified (TIMESTAMPTZ, computed in UTC)? [Consistency, Spec §FR-025]

## Soft-Reference Tolerance

- [x] CHK025 - Are reader-tolerance requirements defined for soft references that dangle into dropped partitions (no error/500/row-drop; rely on denormalized fields)? [Completeness, Spec §FR-021/SC-007]
- [x] CHK026 - Is a dangling-soft-reference check specified that treats absence as expected/tolerated (not corruption) for operator visibility? [Coverage, Spec §FR-022]

## Boundary & Success-Criteria Quality

- [x] CHK027 - Are month/year boundary requirements for partition bounds specified (Dec→Jan, February length)? [Edge Case, Spec §Edge Cases/FR-007]
- [x] CHK028 - Are the success criteria measurable and technology-agnostic (zero missing-partition write failures, 0% bulk DELETE, bounded table size, 100% tolerant reads)? [Measurability, Spec §SC-001..SC-007]
- [x] CHK029 - Is the lead-time requirement for advance partition creation stated strongly enough to guarantee next-month exists before the month begins? [Clarity, Spec §Clarifications/SC-001]

## Notes

- All items validated against spec.md (incl. ## Clarifications) and cross-checked with plan.md/data-model.md. Every item passed; see per-item spec references.
