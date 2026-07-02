# Security & Isolation Requirements Quality Checklist: Auth, API Keys & Workspace Isolation

**Purpose**: Rigorously validate that SPEC-03's security and multi-tenant-isolation requirements are complete, clear, consistent, and measurable before implementation. This is the project's NON-NEGOTIABLE isolation spec.
**Created**: 2026-07-02
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [contracts/](../contracts/)
**Depth**: Rigorous · **Audience**: Reviewer (pre-implementation security gate)

## Password & Login

- [x] CHK001 - Are password-storage requirements complete (password KDF / argon2id, per-user salt, never plaintext, never logged)? [Completeness, Spec §FR-005]
- [x] CHK002 - Is the login-failure contract specified as uniform in BOTH message and timing (no factor disclosure, no account-enumeration timing side-channel via dummy verification)? [Clarity, Spec §FR-006, Edge Cases]
- [x] CHK003 - Are login rate-limit requirements specified along BOTH dimensions (per-account AND per-source-address) with progressive backoff? [Completeness, Spec §FR-007, §SC-009]
- [x] CHK004 - Is the rate-limit / status cache fail-safe behavior specified for cache unavailability (deny/challenge, never silently allow)? [Coverage, Spec §FR-007, Edge Cases, Assumptions]

## Refresh Tokens

- [x] CHK005 - Is at-rest handling specified (stored only as a hash; raw value never persisted)? [Completeness, Spec §FR-008]
- [x] CHK006 - Is rotate-on-use and rejection-of-rotated-reuse specified unambiguously? [Clarity, Spec §FR-009, §SC-002]
- [x] CHK007 - Is atomic rotation under concurrency specified (of two simultaneous exchanges at most one succeeds)? [Clarity, Spec §FR-010, §SC-002]
- [x] CHK008 - Are expiry and logout/revocation specified (expired rejected; sign-out revokes; revoked cannot be exchanged)? [Completeness, Spec §FR-011, §SC-003]

## API Keys

- [x] CHK009 - Is API-key storage specified (high-entropy secret, fast SHA-256 hash + non-secret prefix, NOT a password KDF)? [Completeness, Spec §FR-012]
- [x] CHK010 - Is "shown only once, never retrievable afterward" specified for the full secret? [Clarity, Spec §FR-012, US2 AS-1]
- [x] CHK011 - Is scope enforcement specified (key limited to its scopes; out-of-scope request refused) with a defined scope vocabulary? [Completeness, Spec §FR-013, §SC-004]
- [x] CHK012 - Is revocation specified such that a revoked/non-active key authenticates nothing? [Clarity, Spec §FR-014, §SC-004]
- [x] CHK013 - Is throttled `last_used_at` specified (at most one write per key per minute, buffered, never per-request)? [Measurability, Spec §FR-015, §SC-008]
- [x] CHK014 - Is prefix-collision safety specified (lookup by prefix then verify full-secret hash; a collision must not authenticate the wrong key)? [Coverage, Spec §FR-016, Edge Cases]

## Workspace Context & RLS (NON-NEGOTIABLE)

- [x] CHK015 - Is "exactly one workspace context per request, applied transaction-scoped (pooler-safe) for the request's duration" specified? [Clarity, Spec §FR-017, §SC-001-context]
- [x] CHK016 - Are workspace-scoped repository/query helpers required (require a workspace context; forbid fetch-by-id-alone on workspace-owned models)? [Completeness, Spec §FR-018]
- [x] CHK017 - Is RLS required on the first workspace-owned tables (users, api_keys) enabled in the SAME migration that creates them? [Completeness, Spec §FR-004, §FR-019]
- [x] CHK018 - Is RLS fail-closed behavior specified for BOTH absent AND empty workspace context (zero rows, never all rows)? [Clarity, Spec §FR-019, §SC-005]
- [x] CHK019 - Is it required that RLS blocks cross-workspace rows even when the application-level filter is omitted (defense-in-depth)? [Coverage, Spec §FR-019, §SC-005]
- [x] CHK020 - Is the nullable-workspace SUPER_ADMIN case addressed (not a wildcard RLS bypass; must resolve an explicit workspace per request)? [Edge Case, Spec §FR-002, Edge Cases]
- [x] CHK021 - Is the pre-authentication credential-lookup path's elevated access bounded (confined to credential resolution; unreachable by request-serving business-data queries)? [Coverage, Spec §FR-020a, Edge Cases]

## Isolation Tooling & Tests

- [x] CHK022 - Is a CI guard required that fails the build on any introduced unscoped fetch-by-id / unscoped select on a workspace-owned model? [Completeness, Spec §FR-020, §SC-006]
- [x] CHK023 - Are automated cross-workspace tests required, including the omitted-application-filter case (RLS still blocks)? [Completeness, Spec §FR-021, §SC-005]

## Status Cache

- [x] CHK024 - Is suspension propagation specified as "within the status-cache TTL" (a bounded, measurable window) rather than "immediately"/"eventually"? [Measurability, Spec §FR-022, §SC-007]
- [x] CHK025 - Is "no per-request status database read (status served from cache)" specified as a measurable steady-state property? [Measurability, Spec §FR-022, §SC-007]

## Consistency & Acceptance Criteria

- [x] CHK026 - Do the spec, plan, and constitution agree that users + api_keys are workspace-owned (RLS) while workspaces is the tenant root and refresh_tokens is reached via its owning user? [Consistency, Spec §FR-004, §32, Clarifications]
- [x] CHK027 - Are the SHA-256-for-API-keys vs password-KDF-for-passwords choices consistent and non-contradictory across requirements? [Consistency, Spec §FR-005, §FR-012]
- [x] CHK028 - Are success criteria SC-001–SC-009 objectively measurable (counts/percentages/TTL-bounded/at-most-once)? [Measurability, Spec §SC-001–SC-009]

## Scope Discipline & Dependencies

- [x] CHK029 - Is scope explicitly bounded to auth + api-key endpoints and the 4 identity tables only (no products/variants/competitors/matches/scrape-profiles or their endpoints)? [Boundary, Spec Assumptions, §FR-023]
- [x] CHK030 - Is the first-workspace/SUPER_ADMIN bootstrap path specified (seed/admin path; no public self-service signup)? [Coverage, Spec Assumptions, Clarifications]
- [x] CHK031 - Are the foundation dependencies documented (SPEC-02 emit_rls_policy NULLIF helper + SET LOCAL mechanism; the workspaces.default_*_id columns are FK-less nullable identifiers pending later specs)? [Assumption, Spec Assumptions]
- [x] CHK032 - Are the live-DB/Redis deferrals documented (RLS row denial, cross-workspace blocking, rate-limit/status-TTL/last-used behavior, migration run) with DB/Redis-independent logic verifiable now? [Assumption, Spec Assumptions]

## Notes

- 32 items evaluated against spec.md (as amended) + plan.md + 11 contracts. **32/32 pass.**
- Two gaps surfaced during rigorous evaluation and were fixed before checking:
  1. **Login timing side-channel** — spec required uniform error message but not uniform *timing*; added to §FR-006 + an Edge Case (dummy verification on unknown email). CHK002 then passes.
  2. **Pre-auth RLS-bypass unbounded** — the plan's `crawmatic_auth` BYPASSRLS role (needed for pre-context credential lookups) had no governing *requirement*; added §FR-020a + an Edge Case confining any pre-auth elevated access to credential resolution only. CHK021 then passes.
- ≥80% traceability: every item cites a spec section, success criterion, or edge case.
- Live-DB/Redis behavioral verification is deferred to a Postgres/Redis host; this checklist validates requirement *quality*.
