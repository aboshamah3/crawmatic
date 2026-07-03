# Security & Correctness Requirements Checklist: Scrapyd HTTP Spider MVP

**Purpose**: Validate that the security- and correctness-critical requirements for the spider are
complete, clear, consistent, and measurable — before implementation.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md)
**Depth**: Release-gate · **Audience**: Reviewer (PR)

## Fetch-Time URL Safety (SSRF)

- [x] CHK001 - Is fetch-time URL safety specified as validating the RESOLVED IP at connection time (distinct from save-time host validation)? [Clarity, Spec §FR-005]
- [x] CHK002 - Is re-validation of EVERY redirect hop explicitly required? [Completeness, Spec §FR-005, US2]
- [x] CHK003 - Are scheme-allowlist and userinfo-rejection requirements defined for fetch time? [Coverage, Spec §FR-005, US2]
- [x] CHK004 - Is the injectable resolver/allowlist seam specified such that tests can drive the deny path AND allow local fixtures without weakening production (prod validates real resolved IP, no allowlist)? [Clarity, Spec §Clarifications, §FR-005]
- [x] CHK005 - Is the outcome of an SSRF rejection specified (no `success=true` observation; failure recorded in request_attempt/observation)? [Completeness, Spec §US2, Edge Cases]
- [x] CHK006 - Is the error code / vocabulary for an unsafe-URL rejection defined (traceable to §34)? [Traceability, Spec §US2 / contracts/errors.md]

## Workspace Isolation (RLS)

- [x] CHK007 - Are workspace-scoping requirements defined for ALL spider reads and writes (not just reads)? [Coverage, Spec §FR-002, US1]
- [x] CHK008 - Are DB-level RLS (fail-closed, enabled+forced) requirements specified for the three new tables? [Gap, Spec §FR-012 / plan Constitution II]
- [x] CHK009 - Is behavior specified when a match is not found for the given workspace (skip, never cross-read)? [Edge Case, Spec §Edge Cases]

## Monetary & Extraction Correctness

- [x] CHK010 - Is the monetary type mandated (Decimal / NUMERIC(18,4), floats forbidden end-to-end)? [Clarity, Spec §FR-010]
- [x] CHK011 - Are rejection rules for NaN/Infinity/over-scale/non-positive candidates specified (reject, not round)? [Completeness, Spec §FR-009, US3]
- [x] CHK012 - Is the minimum accepted confidence quantified (default 0.75) and per-method defaults specified (JSON-LD 0.95 / CSS 0.85 / regex 0.75 / single-number 0.40)? [Measurability, Spec §FR-008]
- [x] CHK013 - Are price validation rules (old/installment/discount/shipping, reject_if_text_contains, min/max) sourced from a defined config location? [Clarity, Spec §FR-009]
- [x] CHK014 - Is currency-mismatch handling specified (comparable=false, CURRENCY_MISMATCH, excluded from comparison, no FX)? [Completeness, Spec §FR-011, US3]
- [x] CHK015 - Is the behavior on sub-threshold / not-found extraction specified (success=false, current price NOT overwritten with a bad value)? [Consistency, Spec §FR-014, US3]

## Reactor Safety & Batched Persistence

- [x] CHK016 - Is the reactor-safe DB mechanism decided once and named (sync SQLAlchemy in deferToThread, in scrape-core)? [Clarity, Spec §FR-017, §Clarifications]
- [x] CHK017 - Are batched-flush thresholds quantified (every 50 items or 2s, tunable) with a final flush at spider close? [Measurability, Spec §FR-016, US5]
- [x] CHK018 - Is the "no DB call blocks the reactor thread" constraint stated as a verifiable requirement? [Measurability, Spec §FR-017, US5]

## Authenticated & Idempotent Dispatch

- [x] CHK019 - Is authenticated Scrapyd `schedule.json` dispatch required, with unauthenticated/incorrect calls rejected and not starting a run? [Completeness, Spec §FR-018, US4]
- [x] CHK020 - Is dispatch idempotency specified so a retried schedule of the same job/batch does not double-run? [Coverage, Spec §FR-019, US4]
- [x] CHK021 - Is pass-through of spider arguments (workspace_id, scrape_job_id, match_ids, mode) to the scheduled run specified? [Completeness, Spec §US4]

## Partitioned-Table Correctness

- [x] CHK022 - Are price_observations/request_attempts required to be born monthly-partitioned with the partition key in the PK? [Clarity, Spec §FR-012]
- [x] CHK023 - Is creation of initial partitions (at least current + next month) specified? [Completeness, Spec §FR-012]
- [x] CHK024 - Is tolerance of dangling soft references after partition drop specified for readers? [Edge Case, Spec §Edge Cases, Key Entities]

## Fixtures-Only Testing & Scope Boundary

- [x] CHK025 - Is fixtures-only testing (zero real-competitor network calls) stated as a measurable success criterion? [Measurability, Spec §FR-021, §SC-007]
- [x] CHK026 - Do the fixtures cover each extraction path (JSON-LD, CSS, regex) plus SSRF deny/redirect cases? [Coverage, Spec §Key Entities, US2/US3]
- [x] CHK027 - Is the persist-only boundary explicitly required (no alerts / variant states / alert events / webhooks / price_analysis emission)? [Consistency, Spec §FR-020]
- [x] CHK028 - Is the FR-015 "update job target state" requirement consistent with the scope boundary (its backing table is out of this slice)? [Conflict, Spec §FR-015 vs plan Complexity Tracking]

## Notes

- Items are requirements-quality checks (are the requirements well-written?), not implementation
  verification. Each traces to a spec section or a named gap/conflict marker.
- Items marked incomplete require spec/plan updates before `/speckit-tasks`.
