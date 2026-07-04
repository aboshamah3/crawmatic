# Checklist: Distributed Correctness & Operational Safety

**Purpose**: Validate that the requirements for SPEC-11 (rate limiting & in-flight locks) are complete, clear, consistent, and measurable in the areas most likely to break a distributed, reactor-based scraping system. This tests the *requirements*, not the implementation.
**Created**: 2026-07-04
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md)

## Concurrency & Atomicity

- [x] CHK001 - Is the atomicity guarantee for token/slot acquisition specified so concurrent workers cannot collectively exceed a limit due to a read-modify-write race? [Clarity, Spec §FR-004]
- [x] CHK002 - Are the rate-limit and semaphore key formats specified precisely enough (workspace, domain, access method) to guarantee no cross-key collision? [Completeness, Spec §FR-002/FR-003]
- [x] CHK003 - Is the release semantics for the match lock defined to prevent a slow prior owner from deleting a newer owner's lock (fencing token)? [Clarity, Spec §FR-012]
- [x] CHK004 - Are requirements defined for what "granted vs denied" means at the boundary (exactly at the limit R and at concurrency C)? [Edge Case, Spec §US1]

## TTL & Deadlock-Freedom

- [x] CHK005 - Do requirements mandate a TTL on every coordination key (rate bucket, semaphore slot, match lock) so a crashed process cannot deadlock a domain/match? [Completeness, Spec §FR-005]
- [x] CHK006 - Is the match-lock TTL sizing rule stated relative to the worst-case in-spider wait (backoff + requeue cap), not just fixed numbers? [Clarity, Spec §FR-013]
- [x] CHK007 - Are the differing lock TTLs for HTTP vs browser modes specified? [Completeness, Spec §FR-013]
- [x] CHK008 - Is reclaim-after-crash behavior specified for both semaphore slots and match locks? [Coverage, Spec §US1/§US2 edge cases]

## Reactor Non-Blocking Guarantee

- [x] CHK009 - Is the non-blocking-on-reactor requirement stated as an observable, testable property (no time.sleep, no sync blocking store call on the reactor thread)? [Measurability, Spec §FR-007, §SC-005]
- [x] CHK010 - Is the delay/reschedule mechanism required to be non-blocking (async reschedule) rather than an in-thread sleep? [Clarity, Spec §FR-006]
- [x] CHK011 - Does the plan fix, in one place (scrape-core), whether Redis calls are async or deferToThread, per the Constitution? [Consistency, Plan reactor-seam contract]

## Fail-Safe / Resilience

- [x] CHK012 - Is behavior on Redis unavailability explicitly specified as fail-safe (deny/backoff, never uncontrolled fetch)? [Completeness, Spec §FR-023]
- [x] CHK013 - Is the fail-safe direction for SPEC-11 (fail-closed) distinguished from any prior fail-open Redis usage (e.g., budget counters) to avoid an inconsistency? [Consistency, Conflict-check]
- [x] CHK014 - Are requirements clear that rate/window math must not depend on synchronized wall clocks across processes? [Clarity, Spec §Edge Cases]

## Workspace Isolation

- [x] CHK015 - Is per-workspace independence of all limiter/lock keys stated as a requirement (no cross-workspace interference)? [Completeness, Spec §FR-009]
- [x] CHK016 - Are the key templates consistent in placing `workspace_id` first across rate, semaphore, and lock keys? [Consistency, Spec §FR-002/FR-003/FR-010]

## Requeue Cap & Overflow Bounds

- [x] CHK017 - Are BOTH bounds (max requeue count AND max cumulative in-spider wait) specified, not just one? [Completeness, Spec §FR-017]
- [x] CHK018 - Is the overflow outcome (target state + re-dispatch path) unambiguously defined, including which enum member represents "deferred"? [Clarity, Spec §FR-018, Clarifications]
- [x] CHK019 - Is it specified that an overflowed/re-dispatched target re-enters lock+limiter checks so no double-run occurs? [Coverage, Spec §FR-019]
- [x] CHK020 - Is there a stated ceiling preventing an infinite overflow→re-dispatch→overflow loop? [Edge Case, Spec §Edge Cases]
- [x] CHK021 - Is the jitter range specified as bounded (lower and upper) to prevent both lockstep collisions and unbounded delay? [Clarity, Spec §FR-006, §Edge Cases]

## Observability & Error Codes

- [x] CHK022 - Are the exact structured error codes for the two contention outcomes specified (RATE_LIMITED for overflow, LOCKED_ALREADY_RUNNING for lock skip)? [Completeness, Spec §FR-020/FR-021]
- [x] CHK023 - Is it specified that these codes are reused from the existing enum rather than newly defined (avoiding a duplicate/conflicting definition)? [Consistency, Spec Clarifications]
- [x] CHK024 - Are rate-limit hits required to be observable (logged/counted) for operations, distinct from the persisted target outcome? [Coverage, Spec §FR-022]

## Acceptance Criteria Measurability

- [x] CHK025 - Can each Success Criterion (SC-001..SC-006) be objectively measured without reference to implementation internals? [Measurability, Spec §Success Criteria]
- [x] CHK026 - Is the "no duplicate observation" guarantee (SC-002) expressed as a measurable count rather than a qualitative claim? [Measurability, Spec §SC-002]

## Dependencies & Assumptions

- [x] CHK027 - Are the reused prior-spec assets (access config columns, error-code enum, unique constraint, scrape_dispatch, spider fetch path) documented as dependencies rather than re-created? [Assumption, Spec §Assumptions, Clarifications]
- [x] CHK028 - Is the source of the limit *values* (DomainAccessRule override → AccessPolicy default → built-in default) fully specified, including the no-config fallback? [Completeness, Spec §FR-008]
