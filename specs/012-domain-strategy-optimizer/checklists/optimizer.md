# Requirements Quality Checklist: Domain Strategy Optimizer

**Purpose**: Validate that SPEC-12 requirements are complete, clear, consistent, and measurable
before implementation — a formal pre-implementation gate. Focus: learning/promotion correctness,
workspace isolation, reactor-safety & scale, URL-pattern versioning, rediscovery coverage.
**Created**: 2026-07-05
**Feature**: [spec.md](../spec.md)

## Learning & Promotion Correctness

- [x] CHK001 Is the promotion threshold fully quantified (count, distinct-URL count, confidence, price validity, currency-when-required)? [Clarity, Spec §FR-010]
- [x] CHK002 Are access-method learning and extraction-method learning specified as independent tracks with separate confidences? [Completeness, Spec §FR-008, §FR-011]
- [x] CHK003 Is it specified which attempt outcomes do NOT count toward promotion (sub-threshold confidence, invalid price, missing required currency)? [Coverage, Spec §FR-010, US1 scenario 3]
- [x] CHK004 Is the state machine over profile status (DISCOVERY_REQUIRED→LEARNING→ACTIVE→DEGRADED→DISABLED) defined for every transition the feature triggers? [Completeness, Spec §FR-007]
- [x] CHK005 Can "3 different URLs of the same domain+pattern" be objectively measured (what identifies a distinct URL)? [Measurability, Spec §FR-010]
- [x] CHK006 Is the default confidence threshold (0.85) and its configurability documented without conflicting values across the spec? [Consistency, Spec §FR-010, Assumptions]

## URL Pattern Grouping & Versioning

- [x] CHK007 Are all URL normalization steps enumerated unambiguously (scheme/www/trailing-slash/fragment/query/locale/segments)? [Completeness, Spec §FR-001]
- [x] CHK008 Are ID-like segment criteria specified precisely enough to be testable (all-digits, UUID-like, long mixed alphanumeric, mostly-digits)? [Clarity, Spec §FR-002]
- [x] CHK009 Are the product-path collapse rules enumerated for each known product key? [Completeness, Spec §FR-003]
- [x] CHK010 Is the versioning rule that lookups MUST NOT mix `url_pattern_version` values stated as a hard requirement? [Consistency, Spec §FR-005, §FR-015]
- [x] CHK011 Is the manual override precedence (override beats derived pattern) unambiguous? [Clarity, Spec §FR-006, Edge Cases]
- [x] CHK012 Is the algorithm-version-bump backfill/re-link obligation specified as a requirement, not left implicit? [Gap, Spec §FR-005]

## Workspace Isolation & Data Integrity

- [x] CHK013 Is workspace scoping specified for all three tables, including the transitive path for `strategy_attempt_stats` (no direct workspace_id per §22)? [Completeness, Spec §FR-026]
- [x] CHK014 Is the no-context → zero-rows / cross-workspace-denied behavior stated as a testable requirement for each table? [Measurability, Spec §FR-026, SC-005]
- [x] CHK015 Are both unique constraints (profile key; attempt-stats key) explicitly required? [Completeness, Spec §FR-027]
- [x] CHK016 Is the UUIDv7 identifier requirement stated for all entities? [Completeness, Spec §FR-028]
- [x] CHK017 Are the three tables' write-volume characteristics (learned/rolled-up, non-partitioned) documented to justify NOT partitioning them? [Assumption, Spec Assumptions]

## Reactor-Safety & Scale (Atomic Buffered Stats)

- [x] CHK018 Is the prohibition on per-attempt primary-store writes stated as a hard requirement? [Clarity, Spec §FR-022, SC-003]
- [x] CHK019 Is the atomic flush semantics (single `count = count + delta` per key, no read-modify-write) specified unambiguously? [Clarity, Spec §FR-023]
- [x] CHK020 Is the buffer key `(profile_id, method_type, method_name)` consistent between recording, flush, and decision-read requirements? [Consistency, Spec §FR-022, §FR-024]
- [x] CHK021 Is "decisions read persisted counts + pending buffered deltas" specified so staleness is bounded? [Completeness, Spec §FR-024]
- [x] CHK022 Is the no-blocking-call-on-reactor-thread constraint stated as a verifiable requirement? [Measurability, Spec §FR-025, SC-007]
- [x] CHK023 Are flush trigger points (periodic + job finalization) both specified? [Coverage, Spec §FR-023]

## Consumption (Learned Start)

- [x] CHK024 Is it specified which statuses yield a learned start vs. the default ladder (ACTIVE/LEARNING-with-preferred yes; DISABLED/absent/version-mismatch no)? [Completeness, Spec §FR-013, §FR-014, US2]
- [x] CHK025 Is the fallback-to-default-ladder behavior for an unseen key specified as non-erroring? [Edge Case, Spec §FR-013, Edge Cases]

## Discovery

- [x] CHK026 Is the sample-size bound (3–10) stated as a validated requirement with a defined reject behavior? [Clarity, Spec §FR-016, §FR-019]
- [x] CHK027 Are both discovery triggers (automatic on new key; operator-initiated) specified as converging on one code path? [Consistency, Spec §FR-016, Clarifications]
- [x] CHK028 Is the no-winning-combination outcome specified (run + profile state)? [Edge Case, Spec §FR-018, US3 scenario 4]
- [x] CHK029 Is the post-discovery profile transition (out of DISCOVERY_REQUIRED to LEARNING/ACTIVE) specified? [Completeness, Spec §FR-018, US3 scenario 3]

## Rediscovery & Degradation

- [x] CHK030 Are all rediscovery trigger conditions enumerated and each made measurable (windows/counters defined)? [Coverage, Spec §FR-020, Clarifications]
- [x] CHK031 Is "3 consecutive failures" defined via a concrete counter (recent_failure_count reset rule)? [Clarity, Spec §FR-020, Clarifications]
- [x] CHK032 Is the periodic light re-check specified as able to detect degradation without a full failed batch? [Completeness, Spec §FR-021, US4 scenario 4]

## Dependencies, Assumptions & Traceability

- [x] CHK033 Are the upstream reuse dependencies (SPEC-05 url_pattern, SPEC-06 extraction, SPEC-07 spider/request_attempts, SPEC-10 access/proxy) documented as assumptions? [Assumption, Spec Assumptions]
- [x] CHK034 Is every functional requirement traceable to at least one acceptance scenario or success criterion? [Traceability, Spec §FR/US/SC]
- [x] CHK035 Are the deferred-live-verification assumptions (no Docker/Postgres/Redis/Scrapyd in build env → integration tests skip cleanly) documented? [Assumption, Spec Assumptions]

## Notes

- Check items off as completed: `[x]`
- Each item tests the REQUIREMENTS' quality (completeness/clarity/consistency/measurability/coverage), not the implementation.
