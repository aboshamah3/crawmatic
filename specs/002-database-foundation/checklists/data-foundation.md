# Database-Foundation Requirements Quality Checklist: Database Foundation

**Purpose**: Validate that SPEC-02's requirements for IDs, timestamps, naming, money, enums, RLS-readiness, session hygiene, migrations, and scope discipline are complete, clear, consistent, and measurable — before implementation.
**Created**: 2026-07-02
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [contracts/](../contracts/)
**Depth**: Standard · **Audience**: Reviewer (pre-implementation gate)

## Requirement Completeness

- [x] CHK001 - Are ID-generation requirements complete (application-generated UUIDv7, used as the base-model primary key)? [Completeness, Spec §FR-003]
- [x] CHK002 - Is timestamp discipline fully specified (timezone-aware `TIMESTAMPTZ`, at least created/updated, naive columns forbidden at the base level)? [Completeness, Spec §FR-004]
- [x] CHK003 - Is the constraint/index naming-convention requirement complete across all constraint kinds (unique, index, check, foreign key, primary key)? [Completeness, Spec §FR-001, §FR-002]
- [x] CHK004 - Are money-type requirements complete (`NUMERIC(18,4)`, reject NaN/Infinity, reject over-scale, never float)? [Completeness, Spec §FR-005]
- [x] CHK005 - Is enum representation specified (string-backed, application-validated, no database-native enum types)? [Completeness, Spec §FR-006]
- [x] CHK006 - Is the RLS-ready workspace-scoped base + policy-DDL helper specified (`workspace_id`, enable/force RLS, fail-closed, `SET LOCAL`)? [Completeness, Spec §FR-007]
- [x] CHK007 - Are session/engine-hygiene requirements complete (one lazy engine per process, fork-safe disposal, pooler-safe configuration)? [Completeness, Spec §FR-008, §FR-009, §FR-010]
- [x] CHK008 - Are migration-system requirements complete (one-shot job, direct-to-Postgres, apps never migrate at startup, single linear history, CI multi-head guard)? [Completeness, Spec §FR-011, §FR-012]
- [x] CHK009 - Is a demonstration/smoke requirement present that proves the machinery without introducing real domain tables? [Completeness, Spec §FR-013]
- [x] CHK010 - Is the migration environment's target-metadata + naming-convention honoring specified (so autogenerate reflects deterministic names)? [Completeness, Spec §FR-014]
- [x] CHK011 - Is a basic database-connectivity-check requirement present? [Completeness, Spec §FR-015]
- [x] CHK012 - Is migration-failure atomicity/rollback + downgrade behavior specified? [Completeness, Spec §FR-016, Edge Cases]

## Requirement Clarity

- [x] CHK013 - Is "naive timestamps forbidden at the base level" expressed as an enforceable mechanism rather than prose intent? [Clarity, Spec §FR-004, plan.md TZDateTime]
- [x] CHK014 - Is "naming convention includes ALL constrained columns" concrete enough to disambiguate two multi-column uniques sharing a first column? [Clarity, Spec §FR-002, plan.md verified naming_convention]
- [x] CHK015 - Is "reject over-scale, not silently rounded" unambiguously distinguished from rounding behavior? [Clarity, Spec §FR-005]
- [x] CHK016 - Is RLS "fail closed" defined precisely (absent/empty `app.workspace_id` matches zero rows)? [Clarity, Spec §FR-007]
- [x] CHK017 - Is "migration job connects directly, not through the pooler" made precise (a distinct direct connection target)? [Clarity, Spec §FR-011, plan.md MIGRATION_DATABASE_URL]

## Requirement Consistency

- [x] CHK018 - Do spec, plan, and constitution agree on money scale and finite-only rules? [Consistency, Spec §FR-005, constitution §VII]
- [x] CHK019 - Are engine-hygiene requirements consistent with the SPEC-01 skeleton they extend (no duplicate or contradictory engine setup)? [Consistency, Spec §FR-008, plan.md]
- [x] CHK020 - Is "apps never migrate at startup" consistent with the one-shot-migration-job requirement across all references? [Consistency, Spec §FR-011]
- [x] CHK021 - Is UUIDv7 usage consistent between the ID helper requirement and the base-model primary-key requirement? [Consistency, Spec §FR-003, §FR-001]

## Acceptance Criteria Quality

- [x] CHK022 - Are success criteria SC-001–SC-007 objectively measurable (counts, percentages, pass/fail conditions)? [Measurability, Spec §SC-001–SC-007]
- [x] CHK023 - Is the naming-collision outcome expressed as a concrete count (2 distinct names, 0 collisions)? [Measurability, Spec §SC-003]
- [x] CHK024 - Is "no engine created on import" expressed as a verifiable outcome (0 engines/connections on import)? [Measurability, Spec §SC-005]

## Scenario & Edge Case Coverage

- [x] CHK025 - Is the multiple-migration-heads scenario covered by both a requirement and a CI gate? [Coverage, Spec §FR-012, §SC-006, Edge Cases]
- [x] CHK026 - Is the "application misconfigured to migrate at startup" scenario addressed structurally? [Coverage, Spec §FR-011, Edge Cases]
- [x] CHK027 - Is the "query against a workspace-owned table without a workspace context" scenario (fail closed) addressed at the foundation level? [Coverage, Spec §FR-007, Edge Cases]
- [x] CHK028 - Is a partially-failed migration scenario addressed (transaction rollback to prior revision)? [Coverage, Spec §FR-016, Edge Cases]

## Scope Discipline & Dependencies

- [x] CHK029 - Is out-of-scope explicitly declared (no real domain tables, no auth, no business logic; demonstration/smoke only)? [Boundary, Spec Assumptions, §FR-013]
- [x] CHK030 - Are the key dependencies/assumptions documented (extends SPEC-01 skeleton; live-Postgres acceptance items deferred to a Postgres-capable host; money scale from §19)? [Assumption, Spec Assumptions]

## Notes

- 30 items evaluated against spec.md (as amended) + plan.md + contracts. **30/30 pass.**
- One gap surfaced during evaluation and was fixed before checking: migration-failure atomicity/rollback was unspecified → added Spec §FR-016 + an Edge Case (each migration runs in a transaction; downgrade path provided; non-transactional migrations must document non-atomicity — none in this spec). CHK012/CHK028 then pass.
- ≥80% traceability: every item cites a spec section, plan artifact, or edge case.
- Live-Postgres verification (migration run, connectivity) is deferred to a Postgres-capable host per the no-Docker-daemon constraint; this checklist validates requirement *quality*, not live behavior.
