# Security & Isolation Requirements Checklist: Competitors & Matches

**Purpose**: Validate that the security- and isolation-critical requirements (save-time SSRF/URL-safety, workspace isolation, scope-gating, bulk reject-and-report) are complete, clear, consistent, and measurable BEFORE implementation.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md)
**Focus**: Save-time SSRF/URL-safety, workspace isolation (RLS + app scoping), reference integrity, scope-gating, bulk reject-and-report
**Depth**: Thorough | **Audience**: Implementer + Reviewer

## Save-Time URL Safety — Deny-List Completeness

- [x] CHK001 - Is the set of rejected destination classes enumerated exhaustively (localhost, private 10/8·172.16/12·192.168/16, loopback, link-local 169.254/16·fe80::/10, unique-local fc00::/7, cloud metadata, internal hostnames)? [Completeness, Spec §FR-007]
- [x] CHK002 - Is "internal service hostnames" defined as a concrete deny-list rather than an open-ended phrase (localhost, `*.railway.internal`, compose service names, `*.internal`, `*.local`, metadata hostname)? [Clarity, Spec §Clarifications, §FR-007]
- [x] CHK003 - Are the accepted schemes stated as an explicit allow-list (http, https only) rather than a deny-list of bad schemes? [Clarity, Spec §FR-007]
- [x] CHK004 - Is rejection of embedded credentials (`user:pass@host` / any userinfo) specified as a distinct rule? [Completeness, Spec §FR-008]
- [x] CHK005 - Are both IPv4 and IPv6 forms of each denied range covered (e.g. IPv4-mapped IPv6, `::ffff:169.254.169.254`, shorthand `::1`)? [Edge Case, Gap]
- [x] CHK006 - Is the treatment of IP-literal hosts vs DNS-name hosts distinguished, and is the save-time boundary (no authoritative DNS resolution here) stated explicitly? [Clarity, Spec §FR-007, §FR-009, §Assumptions]
- [x] CHK007 - Is the division of responsibility between save-time checks and fetch-time DNS re-resolution/redirect re-validation (SPEC-07) documented so no gap is assumed covered here? [Consistency, Spec §FR-009, §Assumptions]

## Save-Time URL Safety — Application Points & Behavior

- [x] CHK008 - Is the validator required to apply identically on single create, update, AND bulk-upsert (no path that bypasses it)? [Coverage, Spec §FR-009]
- [x] CHK009 - Is the rejection outcome specified (clear safety error, record not stored) rather than left as "handled"? [Clarity, Spec §Acceptance US2.2]
- [x] CHK010 - Is it stated that an unsafe URL is NEVER persisted under any code path, including partial batch success? [Measurability, Spec §FR-009, §SC-004]
- [x] CHK011 - Are the id-like / normalization thresholds (digit-ratio, length, UUID-like, mostly-digits) either quantified or explicitly deferred behind the algorithm version so ambiguity is bounded? [Ambiguity, Spec §Clarifications, §FR-010]

## URL Normalization & Versioned Pattern

- [x] CHK012 - Are the normalization steps enumerated unambiguously (lowercase host; strip scheme/`www.`/trailing-slash/fragment; query dropped for pattern but retained for normalized URL)? [Clarity, Spec §FR-010, §Edge Cases]
- [x] CHK013 - Is the distinction between the normalized URL (identity) and the derived pattern (grouping) stated so they are not conflated? [Consistency, Spec §Edge Cases, §FR-010]
- [x] CHK014 - Are the "known product path keys" that trigger slug wildcarding enumerated (`/products/`, `/product/`, `/p/`, `/item/`, locale-prefixed variants)? [Completeness, Spec §FR-010]
- [x] CHK015 - Is preservation of locale prefixes (e.g. `/ar/`, `/en/`) specified as an explicit rule distinct from id/slug generalization? [Clarity, Spec §FR-010]
- [x] CHK016 - Is it required that every row storing a pattern also stores the algorithm version, and that lookups never mix versions? [Completeness, Spec §FR-011]
- [x] CHK017 - Is the version-bump backfill explicitly scoped OUT so its absence is intentional, not an oversight? [Coverage, Spec §FR-011, §Assumptions]

## Workspace Isolation — RLS + Application Scoping

- [x] CHK018 - Is dual enforcement (application scoping AND row-level security) required for both competitors and matches, not just one layer? [Completeness, Spec §FR-001, §FR-002]
- [x] CHK019 - Is "fail closed when no workspace context" specified as a concrete requirement with a testable outcome (zero rows)? [Measurability, Spec §FR-002, §SC-007]
- [x] CHK020 - Is RLS required to be enabled in the SAME migration that creates each table (no window where a table exists without RLS)? [Consistency, Spec §FR-001]
- [x] CHK021 - Is the behavior when the application filter is omitted specified (RLS still blocks), so the two layers are independently verified? [Coverage, Spec §Acceptance US4.1, §SC-007]
- [x] CHK022 - Is registration in the workspace-scoped repository helpers AND the CI unscoped-query guard required for both new models? [Completeness, Spec §FR-001, §Acceptance US4.4]

## Workspace-Local Reference Integrity

- [x] CHK023 - Are all match foreign references (product, product variant, competitor) required to resolve within the caller's workspace via composite workspace-local references? [Completeness, Spec §FR-006]
- [x] CHK024 - Is rejection of a reference to another workspace's OR a nonexistent entity specified as a single clear rule? [Clarity, Spec §FR-006, §Acceptance US4.2]
- [x] CHK025 - Is `current_price_id` explicitly defined as a soft reference (no FK), distinct from the enforced references? [Consistency, Spec §FR-006, §Assumptions]
- [x] CHK026 - Are `scrape_profile_id` / `access_policy_id` specified as plain nullable references (no FK until SPEC-06/10) so their unenforced state is intentional? [Assumption, Spec §Assumptions]
- [x] CHK027 - Is the prerequisite that competitors gain `unique(workspace_id, id)` (to support the match composite FK) documented? [Dependency, Spec §Clarifications]

## Scope-Gating (Capability Enforcement)

- [x] CHK028 - Is a required capability specified for every endpoint (competitor read/write, match read/write) with read vs write distinguished? [Completeness, Spec §FR-015]
- [x] CHK029 - Is refusal of a read-only credential attempting a write specified with a measurable outcome (0 successful writes)? [Measurability, Spec §Acceptance US4.3, §SC-008]
- [x] CHK030 - Is it required that EVERY endpoint runs under the request's workspace context (none unscoped)? [Coverage, Spec §FR-015]
- [x] CHK031 - Is the CI guard's failure condition specified (fails build on any introduced unscoped competitor/match query)? [Measurability, Spec §Acceptance US4.4, §SC-008]

## Bulk-Upsert — Reject-and-Report & Set-Based Correctness

- [x] CHK032 - Is the batch error policy for unsafe URLs specified as reject-and-report (not silently dropped, not whole-batch abort)? [Clarity, Spec §FR-013, §Edge Cases]
- [x] CHK033 - Is the shape of the rejection report defined (which records failed and why) rather than left implicit? [Completeness, Spec §FR-013, §Acceptance US3.3]
- [x] CHK034 - Is "set-based / bounded number of statements (never one-per-record)" stated as a measurable requirement? [Measurability, Spec §FR-013, §SC-006]
- [x] CHK035 - Is idempotency on the uniqueness tuple required (re-push updates in place, 0 duplicates), including within a single batch (in-batch duplicate collapse)? [Completeness, Spec §Acceptance US3.2, §Edge Cases, §SC-006]
- [x] CHK036 - Is the match uniqueness key stated identically everywhere it appears `(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)`? [Consistency, Spec §FR-005, §Clarifications]
- [x] CHK037 - Is collision behavior when two raw URLs normalize to the same value for the same variant+competitor specified (treated as same match / upsert, not duplicate)? [Edge Case, Spec §Edge Cases]

## Health Fields, Deletion & Pagination (Security-Adjacent State)

- [x] CHK038 - Are match health fields required to default to safe/pending values and NOT be client-settable at creation? [Completeness, Spec §FR-017, §Acceptance US2 note]
- [x] CHK039 - Is deletion semantics (hard-delete only when no dependent history else archive-by-status; response indicates which) specified for both competitor and match? [Clarity, Spec §FR-016, §Edge Cases]
- [x] CHK040 - Are pagination bounds specified as concrete numbers (default 50, max 500, cursor-based) and is over-max behavior defined (cap, not error)? [Measurability, Spec §FR-014, §Edge Cases]

## Acceptance Criteria Measurability & Traceability

- [x] CHK041 - Does every security/isolation success criterion (SC-004, SC-007, SC-008) have an objectively measurable pass condition (percentages / zero-counts)? [Measurability, Spec §SC-004/007/008]
- [x] CHK042 - Is each FR traceable to at least one acceptance scenario or success criterion, with no orphan security requirement? [Traceability, Spec §Requirements]
- [x] CHK043 - Are the items that require a live PostgreSQL (RLS row denial, cross-workspace, migration run, live upsert) explicitly separated from unit-testable logic, so deferred coverage is intentional not missing? [Coverage, Spec §Assumptions, §Clarifications]
