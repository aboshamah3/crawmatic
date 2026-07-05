# Scheduler Requirements Quality Checklist: Scheduler

**Purpose**: Unit-test the SPEC-13 requirements themselves (completeness, clarity, consistency,
measurability, coverage) for a backend concurrency/scheduling + workspace-isolation feature.
Not a verification/QA list — it validates whether the requirements are well-written.
**Created**: 2026-07-05
**Feature**: [spec.md](../spec.md)

## Concurrency & Duplicate-Run Correctness

- [x] CHK001 Are the requirements for row-level claiming (skip-locked semantics) stated as
  behavior rather than as a specific SQL clause, so they remain testable at the requirements
  level? [Clarity, Spec §FR-008]
- [x] CHK002 Is the exactly-once-per-due-moment guarantee for a rule across concurrent instances
  stated as a measurable outcome? [Measurability, Spec §SC-003]
- [x] CHK003 Is the enqueue-before-commit ordering specified unambiguously, including the explicit
  prohibition of commit-then-enqueue? [Clarity, Spec §FR-012]
- [x] CHK004 Are the requirements consistent about which safety net neutralizes a leaked dispatch
  (idempotent dispatch guard + in-flight match locks) between the concurrency story and the
  assumptions? [Consistency, Spec §FR-014, §Assumptions]
- [x] CHK005 Is the crash-after-claim-before-commit recovery behavior (rollback → lock release →
  next_run_at unchanged → re-claim) fully specified? [Completeness, Spec §US3 AS-2, §Edge Cases]
- [x] CHK006 Is the prohibition of a global/advisory pass-lock — and the narrow allowance of a
  per-rule transaction-scoped advisory lock — stated without contradiction? [Consistency, Spec §FR-009]
- [x] CHK007 Do the requirements state the intended bias (prefer a possible duplicate over a
  missed run) as a design rule rather than leaving it implicit? [Clarity, Spec §FR-014, §US3 AS-3]
- [x] CHK008 Are requirements defined for whether one poison/failing rule in a batch may prevent
  other claimed rules in the same pass from firing (batch vs per-rule transaction boundary)? [Gap]

## Scheduling-Cadence Correctness

- [x] CHK009 Is the mutual exclusivity of cron_expression and interval_minutes (exactly one,
  never neither/both) stated as a hard validation rule? [Completeness, Spec §FR-003]
- [x] CHK010 Is the cron dialect and evaluation timezone explicitly pinned (5-field, UTC) so the
  requirement is unambiguous? [Clarity, Spec §Assumptions]
- [x] CHK011 Is the interval_minutes semantic (next run = run_time + interval) explicitly
  specified rather than left to interpretation? [Clarity, Spec §Assumptions]
- [x] CHK012 Are the requirements for when next_run_at is (re)computed defined for both
  create/update and each successful run? [Completeness, Spec §FR-006]
- [x] CHK013 Is the downtime-backlog behavior specified as "fire once, advance to next FUTURE
  occurrence" (no per-missed-interval catch-up) in measurable terms? [Measurability, Spec §FR-016, §SC-005]
- [x] CHK014 Are all scheduling timestamps required to be timezone-aware (TIMESTAMPTZ) and compared
  against a timezone-aware now? [Completeness, Spec §FR-018]
- [x] CHK015 Are validation-error requirements defined for malformed cron expressions and
  non-positive interval values? [Edge Case, Gap]
- [x] CHK016 Is the meaning/limited role of `priority` (advisory, not affecting claim ordering)
  stated to avoid ambiguity with next_run_at ordering? [Ambiguity, Spec §Assumptions]

## Workspace Isolation & RLS

- [x] CHK017 Are both layers of isolation (application-level scoping AND RLS) required for
  refresh_rules, with RLS present from the first migration? [Completeness, Spec §FR-005]
- [x] CHK018 Is the cross-workspace denial requirement stated so it holds even when an application
  filter is accidentally omitted? [Coverage, Spec §FR-005, §SC-004]
- [x] CHK019 Is the BYPASSRLS system-session deviation (used for the cross-tenant claim) bounded
  by an explicit requirement that app-level scoping is retained for every job/target read/write?
  [Clarity, Consistency, Plan Complexity Tracking]
- [x] CHK020 Are the requirements consistent that the CRUD (API) path never uses the BYPASSRLS
  session — only the scheduler claim does? [Consistency, Gap]
- [x] CHK021 Is refresh_rules explicitly required to be registered among workspace-owned models so
  the unscoped-query CI guard covers it? [Coverage, Gap]

## Scope → Match Resolution

- [x] CHK022 Are resolution requirements defined for all six scopes (WORKSPACE, COMPETITOR,
  PRODUCT, VARIANT, PRODUCT_GROUP, MATCH)? [Completeness, Spec §FR-002, §FR-010]
- [x] CHK023 Is "active matches" defined precisely enough that the resolved target set is
  unambiguous for each scope? [Clarity, Spec §FR-010]
- [x] CHK024 Is the requirement to reuse the existing manual-run scope→match resolution (rather
  than reimplement it) stated? [Consistency, Spec §FR-010, §FR-011]
- [x] CHK025 Is the zero-active-matches behavior (advance schedule, no wasted dispatch, never
  perpetually re-selected) fully specified and measurable? [Completeness, Measurability, Spec §FR-015, §SC-006]
- [x] CHK026 Is the required scope→target-id pairing (WORKSPACE needs none; others need their id;
  mismatch rejected) specified as a validation rule? [Completeness, Spec §FR-002, §US1 AS-6]
- [x] CHK027 Are requirements defined for deleting a scope target (product/variant/group/
  competitor/match) referenced by a rule — that it neither blocks the delete nor dangles? [Edge Case, Spec §FR-020]
- [x] CHK028 Is the created job's type=SCHEDULED and source=SCHEDULER stated as a firm requirement
  (not just an example)? [Clarity, Spec §FR-011]

## Cross-Cutting Requirement Quality

- [x] CHK029 Is the scraping-free constraint (no Scrapy/Twisted/Playwright in app_shared or the
  scheduler image) stated as an enforceable requirement? [Completeness, Spec §FR-019]
- [x] CHK030 Is the no-hot-row-writes constraint scoped clearly to which writes are permitted
  (rule-row updates + standard job/target inserts)? [Clarity, Spec §FR-017]
- [x] CHK031 Are the CRUD operations enumerated completely (create/read/list/update/delete +
  enable-disable) with workspace scoping applied to each? [Completeness, Spec §FR-004]
- [x] CHK032 Do the acceptance scenarios trace cleanly to functional requirements and success
  criteria without orphaned or contradictory items? [Traceability, Consistency]
- [x] CHK033 Are the assumptions (cadence semantics, duplicate tolerance, advisory priority,
  multi-instance, backend-only) all validated against the master doc rather than invented? [Assumption]

## Notes

- This checklist validates requirement quality; live behavioral verification is handled by the
  integration tests authored during implement (which skip cleanly without a live DB).
- Check items off as completed: `[x]`
