# Orchestration Correctness & Concurrency Checklist: Jobs & Orchestration

**Purpose**: Validate that the SPEC-08 requirements/plan are complete, clear, consistent, and measurable for a backend orchestration feature — with emphasis on workspace isolation, idempotency/concurrency, data integrity, and testability. These items test the *requirements writing*, not the implementation.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md)

## Workspace Isolation

- [x] CHK001 Are workspace-isolation requirements defined for BOTH new tables (scrape_jobs, scrape_job_targets) at app-scoping AND Postgres RLS layers? [Completeness, Spec §FR-004]
- [x] CHK002 Is the zero-context read behavior ("no workspace context → zero rows") stated as a testable requirement, not just implied? [Clarity, Spec §FR-004, SC-006]
- [x] CHK003 Are cross-workspace *write*/create rejection requirements specified for the run endpoints (not just reads)? [Coverage, Spec §Edge Cases, FR-006]
- [x] CHK004 Is the not-found behavior for a match/variant that exists only in another workspace unambiguously specified (not-found vs forbidden)? [Clarity, Spec §FR-006/FR-007, Edge Cases]
- [x] CHK005 Do the status/results read endpoints (FR-008/FR-009) explicitly require workspace scoping consistent with FR-004? [Consistency, Spec §FR-008/FR-009]

## Idempotent Dispatch & Node Selection

- [x] CHK006 Is the idempotency guarantee for duplicate dispatch delivery stated as a binding, measurable requirement (exactly one Scrapyd run per batch)? [Measurability, Spec §FR-013, SC-003]
- [x] CHK007 Is the idempotency-guard *key* (job/batch identity) specified precisely enough to implement without ambiguity? [Clarity, Spec §FR-013]
- [x] CHK008 Are deterministic node-selection requirements defined such that two retries of one batch resolve to the same node? [Completeness, Spec §FR-014, US3-AS4]
- [x] CHK009 Is the relationship between the idempotency guard and node selection consistent (a re-dispatch must not both re-run AND land on a second node)? [Consistency, Spec §FR-013/FR-014]
- [x] CHK010 Is the storage location of the idempotency guard resolved (or explicitly deferred to planning with binding behavior retained)? [Ambiguity, Spec §Clarifications]

## Stalled-Batch Detection & Re-dispatch

- [x] CHK011 Are the conditions that classify a batch as "stalled" specified (targets never progressed past a configured timeout)? [Clarity, Spec §FR-015, US3-AS3]
- [x] CHK012 Is it a stated requirement that re-dispatch must NOT double-run already-progressed or in-flight-locked targets? [Completeness, Spec §FR-015]
- [x] CHK013 Is the stall-timeout value defined or explicitly deferred to configuration (with the detect+re-dispatch behavior still binding)? [Ambiguity, Spec §Clarifications]
- [x] CHK014 Are the guards protecting re-dispatch (same idempotency guard + match locks/target-state check) consistent with the assumptions about lock availability (SPEC-11 not yet built)? [Consistency, Spec §Assumptions, FR-015]
- [x] CHK015 Is node-loss (Scrapyd per-node non-durable pending queue) documented as the driving scenario so the requirement's rationale is traceable? [Traceability, Spec §Edge Cases]

## Counters & Deterministic Finalization

- [x] CHK016 Is the "aggregate counters from targets, never per-target increments on the job row" rule stated as a hard requirement with a measurable outcome? [Measurability, Spec §FR-018, SC-004]
- [x] CHK017 Is the finalization status rule fully specified for all target outcome mixes (all-success → COMPLETED, mixed → PARTIAL_FAILED, none-success → FAILED)? [Completeness, Spec §FR-019, US3-AS2]
- [x] CHK018 Is the treatment of SKIPPED targets in the finalization rule unambiguous (do skips block COMPLETED / count toward PARTIAL_FAILED)? [Clarity, Spec §FR-019, Edge Cases]
- [x] CHK019 Is the zero-target job outcome specified as a single deterministic result (COMPLETED, total_targets=0, no dispatch) with no residual either/or? [Consistency, Spec §FR-020, US2-AS4]
- [x] CHK020 Are the lifecycle timestamps (started_at set when work begins, completed_at at terminal state) defined precisely enough to verify? [Clarity, Spec §FR-019]
- [x] CHK021 Is total_targets defined as exactly the count of created targets, with a consistency link to per-target aggregation? [Consistency, Spec §FR-020, FR-002]

## Batching Strategy

- [x] CHK022 Are the batch grouping keys (workspace, competitor/domain, scrape mode HTTP/BROWSER) enumerated as requirements? [Completeness, Spec §FR-011]
- [x] CHK023 Is the HTTP batch-size bound (50–200) stated as a measurable requirement, and is the "not one Scrapyd job per URL at scale" anti-goal captured? [Measurability, Spec §FR-011, SC-008]
- [x] CHK024 Is the payload the dispatch call must carry (workspace_id, scrape_job_id, match_ids, authenticated) specified unambiguously? [Clarity, Spec §FR-012]
- [x] CHK025 Are BROWSER-mode batches acknowledged/bounded even though browser scraping is a later spec (grouping key present, sizing not over-specified)? [Coverage, Spec §FR-011, Assumptions]

## Fork-Safety & Worker Lifecycle

- [x] CHK026 Is the Celery fork-safety requirement (dispose inherited DB engine on worker_process_init before first use) stated explicitly? [Completeness, Spec §FR-016]
- [x] CHK027 Is it clear whether FR-016 requires new code or is satisfied by an existing hook (avoiding a duplicate/ambiguous requirement)? [Clarity, Spec §FR-016, plan.md]

## Data Model & Migration Integrity

- [x] CHK028 Are the exact columns/enums for scrape_jobs and scrape_job_targets specified (matching PROJECT_SPEC §22) with no missing fields? [Completeness, Spec §FR-001/FR-002/FR-003]
- [x] CHK029 Is the unique(scrape_job_id, match_id) constraint stated as a requirement and tied to the no-duplicate-target guarantee? [Traceability, Spec §FR-002, SC-002]
- [x] CHK030 Is the single-head Alembic migration requirement (chain onto current head, remain single-head) explicit? [Completeness, Spec §FR-005]
- [x] CHK031 Are the enum value sets (type/status/source/target-status) closed and consistent between spec, data-model, and plan? [Consistency, Spec §FR-003, data-model.md]
- [x] CHK032 Is rollback/downgrade behavior for the migration addressed or explicitly deferred? [Coverage, Gap, Spec §FR-005]

## Acceptance Criteria & Testability

- [x] CHK033 Are all success criteria (SC-001…SC-008) measurable without reference to implementation internals? [Measurability, Spec §Success Criteria]
- [x] CHK034 Does every functional requirement map to at least one acceptance scenario or success criterion? [Traceability, Spec §Requirements]
- [x] CHK035 Is the async contract (run endpoints return job id without blocking on the scrape) specified as a verifiable outcome? [Clarity, Spec §SC-001, US1]
- [x] CHK036 Is the "unit tests + integration tests that skip cleanly with no live infra" verification strategy stated as a requirement/assumption? [Completeness, Spec §Assumptions]
- [x] CHK037 Are the boundaries with adjacent specs (SPEC-07 spider/client reuse, SPEC-09 price analysis out of scope, SPEC-11 locks/rate-limiting) documented to prevent scope leakage? [Consistency, Spec §Assumptions]

## Dependencies & Assumptions

- [x] CHK038 Is the reuse of the existing authenticated Scrapyd client (not re-implementing scraping) documented as a dependency? [Dependency, Spec §Assumptions]
- [x] CHK039 Is the assumption about lock availability (idempotency guard + unique constraint provide safety where full locks are absent) validated and non-conflicting? [Assumption, Spec §Assumptions, FR-015]
- [x] CHK040 Are provenance requirements (requested_by=principal, type=MANUAL, source=API for direct endpoints) consistent between the clarification, FR-010, and the enum definitions? [Consistency, Spec §FR-010, Clarifications]

## Notes

- All items evaluated against spec.md (with Clarifications), plan.md, research.md, data-model.md, and contracts/.
- Result: 40/40 pass. The spec pins previously-ambiguous items (provenance, zero-target outcome) via the Clarifications session; stall-timeout value and idempotency-guard storage are explicitly deferred to planning with binding behavior retained (resolved in research.md D2/D4). No unresolved requirement-quality gaps block `/speckit-tasks`.
- CHK032 (migration downgrade): spec requires only forward single-head migration; downgrade parity is handled per the project's established Alembic convention (prior specs authored reversible migrations) — noted, not a spec gap.
