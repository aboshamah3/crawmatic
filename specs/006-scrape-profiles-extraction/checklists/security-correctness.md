# Security & Correctness Checklist: Scrape Profiles & Extraction Rules

**Purpose**: Requirements-quality gate ("unit tests for the English") for the DB-driven scrape-profile config layer — dual-scope isolation/RLS, config-resolution correctness + caching invariants, profile-validation completeness, assignment/bulk safety, and deferred-verification honesty.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md)

## Dual-Scope Isolation & RLS (Requirement Completeness / Consistency)

- [x] CHK001 Are read-visibility requirements for global (`workspace_id IS NULL`) profiles explicitly specified as readable by every workspace? [Completeness, Spec §FR-021, US2-AC3, Edge Cases]
- [x] CHK002 Are write/manage restrictions on global profiles (not creatable/editable/deletable via a tenant write path) unambiguously stated? [Clarity, Spec §FR-021, Edge Cases]
- [x] CHK003 Are workspace-owned-profile isolation requirements stated to match the RLS + workspace-scoped-helper guarantees of prior entities? [Consistency, Spec §FR-004, SC-007]
- [x] CHK004 Is the boundary between "own workspace", "global", and "another workspace" defined for every read and write path? [Coverage, Spec §FR-004/FR-013/FR-021]

## Config-Resolution Correctness (Clarity / Edge Cases)

- [x] CHK005 Is the full resolution precedence order (match → domain-strategy → competitor → workspace → global) documented without ambiguity? [Clarity, Spec §FR-014, US3-AC1/AC2]
- [x] CHK006 Is the terminal "no profile resolved" outcome defined as an explicit non-error sentinel (not an error, not an arbitrary row)? [Completeness, Spec §FR-016, US3-AC3]
- [x] CHK007 Is the domain-strategy step's absent-table behavior defined as a clean no-op that falls through to the competitor default? [Edge Case, Spec §FR-015, US3-AC6]
- [x] CHK008 Are dangling / cross-workspace assignment references specified as "treated as unset → fall through" during resolution? [Edge Case, Spec §FR-017, Edge Cases]
- [x] CHK009 Is precedence behavior specified for every combination of set/unset levels (not just the all-set case)? [Coverage, Spec §US3-AC2, SC-003]

## Resolution Performance & Caching Invariants (Measurability)

- [x] CHK010 Is the N+1-avoidance requirement made measurable via the grouping key `(competitor_id, url_pattern)` and the 10k–20k-match scale? [Measurability, Spec §FR-018, SC-004]
- [x] CHK011 Are the cache key `(workspace_id, competitor_id, url_pattern)`, a short-TTL staleness bound, and invalidation-on-relevant-write all specified? [Completeness, Spec §FR-019, US3-AC5]
- [x] CHK012 Is the acceptable cache-staleness window bounded and objectively verifiable (reflect writes within TTL, or immediately if invalidated)? [Measurability, Spec §SC-005, Edge Cases]

## Profile-Validation Completeness (Completeness / Coverage)

- [x] CHK013 Are enum-validation requirements enumerated with the exact allowed sets for `mode`/`adapter_key`/`variant_strategy`? [Completeness, Spec §FR-005, FR-001]
- [x] CHK014 Is regex validation (must compile + catastrophic-backtracking screen) required for every `*_regex` field, given later execution against untrusted HTML? [Coverage, Spec §FR-006, Edge Cases]
- [x] CHK015 Is the cookie guardrail specified with a reject-boundary (session/authentication cookies rejected; non-identifying technical cookies allowed)? [Clarity, Spec §FR-007, Edge Cases]
- [x] CHK016 Are `validation_rules` constraints fully enumerated (3-letter `required_currency`; finite non-negative `min_price`/`max_price` with scale ≤ 4 and `min ≤ max`; text fields are lists of strings)? [Completeness, Spec §FR-008, US4-AC3]
- [x] CHK017 Are `confidence_rules` constraints specified ([0,1] bounds for per-method, minimum-accepted, and promotion values)? [Completeness, Spec §FR-009, US4-AC3]
- [x] CHK018 Are money-correctness requirements consistent with §19 (Decimal, reject `NaN`/`Infinity`, reject over-scale rather than round)? [Consistency, Spec §FR-022, Edge Cases]
- [x] CHK019 Is round-trip fidelity ("read back exactly as stored" after validation) required for all stored JSON bundles? [Completeness, Spec §FR-010, SC-001]
- [x] CHK020 Are the DB-tunable default confidence values enumerated and their fallback-when-omitted behavior specified (not hardcoded in the extractor)? [Completeness, Spec §FR-011, US4-AC2]

## Assignment & Bulk Safety (Coverage / Completeness)

- [x] CHK021 Is cross-workspace assignment rejection required at all three assignment points (match / competitor default / workspace default)? [Coverage, Spec §FR-012/FR-013, US2]
- [x] CHK022 Is clearing an assignment (null) defined as permitted with a defined fall-through consequence for resolution? [Edge Case, Spec §FR-013, US2-AC4]
- [x] CHK023 Is the bulk create/upsert contract (valid rows upserted as a set, invalid rows rejected-and-reported, batch never aborted) specified with its `(workspace_id, name)` conflict key? [Completeness, Spec §FR-020, SC-008]
- [x] CHK024 Is profile-name uniqueness scope defined, including how the NULL-workspace global case is handled? [Clarity, Spec §FR-003]
- [x] CHK025 Are profile-deletion safety requirements defined so no assignment can be left pointing at a nonexistent profile? [Completeness, Spec §FR-023, Edge Cases]

## Dependencies, Assumptions & Scope (Traceability)

- [x] CHK026 Are the deferred live-Postgres/Redis verifications and the no-Docker-daemon constraint documented as assumptions? [Assumption, Spec §Assumptions]
- [x] CHK027 Are out-of-scope boundaries (access policies/proxies → SPEC-10; spider execution → SPEC-07; domain-strategy tables → SPEC-12) explicitly declared? [Scope, Spec §Input/Clarifications/Assumptions]
- [x] CHK028 Is the assumption that the three assignment columns already exist (SPEC-03/05) stated and validated? [Assumption, Spec §Assumptions, US2]

## Notes

Validation performed 2026-07-03 against spec.md (FR-001..023, SC-001..008) and plan.md. **All 28 items pass** — every requirement is present, clear, and testable at the spec level. No spec/plan edits were required.

Plan-level deferrals that are *correctly* left out of the spec (mechanism, not requirement — each is pinned in a plan.md contract, so requirements remain complete):
- CHK015: the concrete session/auth cookie-name deny heuristic → `contracts/profile-validation.md`.
- CHK024: the NULL-workspace global uniqueness mechanism (partial unique indexes) → `contracts/models-scrape-profiles.md` / `data-model.md`.
- CHK011/CHK012: the exact TTL value (`PROFILE_RESOLUTION_CACHE_TTL_SECONDS = 30`) → `plan.md` / `contracts/config-resolution.md`.
- CHK025: block-vs-null delete policy resolved in plan as FK `ON DELETE SET NULL` → `contracts/migration-scrape-profiles.md`.

Requirements-quality gate: **PASS**. Spec is ready for `/speckit-tasks`.
