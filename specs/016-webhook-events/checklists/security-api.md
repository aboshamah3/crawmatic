# Security & API/Data-Integrity Requirements Checklist: Webhook Events

**Purpose**: Validate the quality (completeness, clarity, consistency, measurability, coverage) of
the SPEC-16 requirements around SSRF safety, workspace isolation, auth scopes, poll-API correctness,
partitioning/retention, decoupled event creation, and the v1 no-delivery boundary — before implementation.
**Created**: 2026-07-06
**Feature**: [spec.md](../spec.md)

## SSRF / URL-Safety Requirements

- [x] CHK001 Are the exact URL-rejection rules (scheme, private/loopback/link-local/unique-local ranges, metadata endpoints, internal hostnames, userinfo) enumerated in requirements rather than left to "validate the URL"? [Completeness, Spec §FR-002]
- [x] CHK002 Is it specified that validation occurs against the *resolved IP*, not only the literal string, to defeat DNS-based bypass? [Clarity, Spec §FR-002, Edge Cases]
- [x] CHK003 Is the requirement to *reuse the existing* competitor-URL validator (rather than a new divergent one) stated explicitly? [Consistency, Spec §FR-003]
- [x] CHK004 Are the save-time validation points (both create AND update) both covered by requirements? [Coverage, Spec §FR-002/FR-004]
- [x] CHK005 Is delivery-time re-validation explicitly scoped OUT of v1 (deferred with the dispatcher) so its absence is intentional, not a gap? [Scope Boundary, Spec §Assumptions]
- [x] CHK006 Is the observable outcome of a rejected URL specified (request rejected, nothing persisted) and measurable? [Measurability, Spec §FR-002, §SC-002]

## Workspace Isolation / RLS Requirements

- [x] CHK007 Are workspace-scoping requirements defined for BOTH tables (webhook_endpoints and webhook_events)? [Completeness, Spec §FR-016]
- [x] CHK008 Is fail-closed behavior for a no-workspace-context session explicitly required to return zero rows (not an error, not another workspace's data)? [Clarity, Spec §FR-016, Edge Cases]
- [x] CHK009 Is the requirement that both RLS *and* application-level scoping apply (defense in depth) stated, consistent with the platform's isolation principle? [Consistency, Spec §FR-016, Constitution II]
- [x] CHK010 Is cross-workspace behavior specified distinctly for direct fetch (404) vs list (absent)? [Clarity, Spec §US1 AS3 / §US2 AS5]
- [x] CHK011 Is workspace isolation coverage stated as objectively verifiable (a caller retrieves zero of another workspace's rows for both tables)? [Measurability, Spec §SC-004]

## Auth Scope Requirements

- [x] CHK012 Are the read vs write scope boundaries (webhooks:read for list/get; webhooks:write for create/update/delete) unambiguously mapped per operation? [Clarity, Spec §FR-017]
- [x] CHK013 Is the requirement that the scopes be registered in the platform scope catalog (grantable to API keys) captured — guarding the recurring "scope missing from enum" class of defect? [Completeness, Spec §FR-017]
- [x] CHK014 Is it specified that webhooks:read alone must NOT permit write operations? [Consistency, Spec §US2 AS6]

## Poll API / Pagination Requirements

- [x] CHK015 Is deterministic, stable ordering required (not just "return events")? [Clarity, Spec §FR-013]
- [x] CHK016 Is the "walk the entire backlog exactly once, no duplicates, no gaps" property stated as a testable requirement, including across monthly partition boundaries? [Measurability, Spec §FR-013, §SC-001]
- [x] CHK017 Are page-size bounds (default + maximum) required rather than unbounded? [Completeness, Spec §FR-014]
- [x] CHK018 Is event_type filtering specified as a first-class list capability? [Coverage, Spec §FR-014]
- [x] CHK019 Are the not-found (unknown id in workspace) and invalid/stale-cursor error behaviors both specified rather than left undefined? [Edge Case, Spec §FR-015]
- [x] CHK020 Is the empty-workspace / past-the-end poll defined as a normal empty result (not an error)? [Edge Case, Spec §Edge Cases]

## Data Model / Partitioning Requirements

- [x] CHK021 Is webhook_events required to be *born* monthly-partitioned by created_at (never created plain and converted)? [Clarity, Spec §FR-007]
- [x] CHK022 Is it required that the primary key include the partition key (created_at)? [Completeness, Spec §FR-007]
- [x] CHK023 Are the webhook_events fields (id, workspace_id, event_type, payload, status, created_at, delivered_at) fully enumerated with nullability? [Completeness, Spec §FR-006]
- [x] CHK024 Is soft-reference-only (no hard FK into the partitioned table; readers tolerate dropped-partition references) stated as a requirement? [Consistency, Spec §FR-019]
- [x] CHK025 Is a bound on payload size specified so a producer cannot store an unbounded blob? [Edge Case, Spec §Edge Cases]

## Retention / Maintenance Requirements

- [x] CHK026 Is the 90-day retention default stated explicitly and quantified? [Clarity, Spec §FR-018]
- [x] CHK027 Is retention required to be implemented as partition drop (never bulk DELETE)? [Consistency, Spec §FR-018]
- [x] CHK028 Is the requirement to reuse the existing SPEC-15 maintenance job/registry — and NOT add a new scheduler or maintenance job — stated? [Scope Boundary, Spec §FR-018, §SC-006]
- [x] CHK029 Is continued poll availability of in-retention events during a partition drop specified? [Coverage, Spec §Edge Cases, §SC-006]

## Decoupled Event Creation Requirements

- [x] CHK030 Are the three source triggers (alert-state transition, scrape-job status change, strategy change) each enumerated with the expected event as an outcome? [Completeness, Spec §FR-008]
- [x] CHK031 Is "exactly one event per source change, attributed to the correct workspace, with a payload identifying the affected entity" stated as a measurable requirement? [Measurability, Spec §FR-008, §SC-003]
- [x] CHK032 Is decoupling required so event creation never blocks and never fails/rolls back the source operation? [Clarity, Spec §FR-009, §SC-005]
- [x] CHK033 Is event-creation work required to be retriable/resilient to duplication without corrupting source state? [Coverage, Spec §FR-009, §US3 AS4]

## v1 No-Delivery Scope Boundary

- [x] CHK034 Is the absence of automatic delivery/dispatch/retry/signing in v1 stated as an explicit out-of-scope boundary? [Scope Boundary, Spec §FR-010]
- [x] CHK035 Is the created-event default status (recorded/not-delivered) and delivered_at=null specified and consistent with the no-delivery boundary? [Consistency, Spec §FR-010/FR-011, §SC-007]
- [x] CHK036 Is the secret's v1 handling (stored encrypted, never returned raw, unused) specified so its presence is intentional, not an unbuilt gap? [Clarity, Spec §FR-005, §Assumptions]

## Dependencies & Assumptions

- [x] CHK037 Are the reuse dependencies (existing SSRF validator, SPEC-15 maintenance machinery, Celery webhook_events queue, existing scope catalog) documented as validated assumptions? [Assumption, Spec §Assumptions]
- [x] CHK038 Is the assumption that events are workspace-wide (not endpoint-filtered) in v1 stated, resolving the potential conflict with endpoint event_types? [Conflict, Spec §Assumptions]

## Notes

- Check items off as completed: `[x]`
- Items are "unit tests for the requirements" — they validate whether the spec is well-written, not whether code works.
