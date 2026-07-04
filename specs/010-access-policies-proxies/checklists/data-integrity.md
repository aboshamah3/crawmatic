# Data Integrity Requirements Quality Checklist: Access Policies, Proxies & Request Attempts

**Purpose**: Validate that the data-model, partitioning, isolation, and attempt-logging
requirements are complete, clear, consistent, and measurable before implementation.
**Created**: 2026-07-04
**Feature**: [spec.md](../spec.md)

## Entity & Attribute Completeness

- [x] CHK001 Are all attributes for access_policies, proxy_providers, and domain_access_rules enumerated with their allowed enum values? [Completeness, Spec §FR-001, FR-002, FR-004]
- [x] CHK002 Is the `access_method` enum constrained to a defined value set on request attempts? [Clarity, Spec §FR-012, Clarifications]
- [x] CHK003 Are the structured error codes for attempt classification enumerated? [Completeness, Spec §FR-013]
- [x] CHK004 Are the access-policy strategy values enumerated and mapped to attempt-sequence behavior? [Clarity, Spec §FR-001, FR-008]

## Partitioning & Scale

- [x] CHK005 Is the request-attempts store required to be monthly-partitioned from birth (not converted later)? [Completeness, Spec §FR-014]
- [x] CHK006 Is the requirement that the primary key includes the partition key stated? [Clarity, Spec §FR-014]
- [x] CHK007 Is the soft-reference (no foreign key) requirement for request_attempts documented, with reader tolerance for dangling references? [Completeness, Spec §FR-014, Edge Cases]
- [x] CHK008 Is the expected data volume / scale assumption (millions/month/workspace) recorded to justify partitioning? [Measurability, Spec §SC-006, US3]

## Attempt Logging Correctness

- [x] CHK009 Is "one attempt row per fetch attempt" specified as an exact-count requirement (N attempts → N rows)? [Measurability, Spec §SC-002, FR-012]
- [x] CHK010 Are the captured fields per attempt (access_method, proxy provider/country, status, timing, success, error) fully enumerated? [Completeness, Spec §FR-012]
- [x] CHK011 Is the requirement that attempt writes are off-reactor and batched (non-blocking) stated? [Completeness, Spec §FR-015, SC-006]

## Resolution & Override Consistency

- [x] CHK012 Is the effective-policy precedence (domain rule over workspace default; URL-pattern over domain-only) unambiguously defined? [Clarity, Spec §FR-007]
- [x] CHK013 Is the tie-break for multiple matching domain rules specified? [Edge Case, Spec §Edge Cases]
- [x] CHK014 Is the behavior for a disabled domain rule (ignored; default applies) defined? [Edge Case, Spec §Edge Cases]
- [x] CHK015 Is the `max_retries = 0` boundary (exactly one attempt) specified? [Edge Case, Spec §Edge Cases]
- [x] CHK016 Is graceful degradation defined when a policy references a disabled/deleted proxy provider? [Edge Case, Spec §Edge Cases]

## Isolation Data Integrity

- [x] CHK017 Is the workspace-scoping rule (null = global default, non-null = tenant) consistent across all four entities and their read/write semantics? [Consistency, Spec §FR-006]

## Notes

- Check items off as completed: `[x]`
- Items test requirement quality, not implementation.
