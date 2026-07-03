# Monetary / Determinism / Data-Integrity Checklist: Current Prices & Alert Logic

**Purpose**: Validate that SPEC-09's requirements are complete, unambiguous, consistent, and measurable — with emphasis on monetary correctness, deterministic alert computation, event-transition integrity, dedup/concurrency, and workspace isolation. These items test the *requirements writing*, not the implementation.
**Created**: 2026-07-03
**Feature**: [spec.md](../spec.md)

## Alert Decision Tree (§23)

- [x] CHK001 Are all 8 ordered steps of the decision tree specified in exact evaluation order? [Completeness, Spec §FR-007]
- [x] CHK002 Are all 6 alert types (NO_COMPETITOR_DATA, RISK, HIGH_PRICE, CHANCE_TO_INCREASE_PRICE, NORMAL, CLOSE_TO_COMPETITORS) enumerated and each reachable from a defined step? [Completeness, Spec §FR-004/FR-007]
- [x] CHK003 Is the exact severity map (RISK=CRITICAL, HIGH_PRICE=HIGH, CHANCE_TO_INCREASE_PRICE=MEDIUM, CLOSE_TO_COMPETITORS=MEDIUM, NO_COMPETITOR_DATA=LOW, NORMAL=NONE) specified as the sole source of severity? [Clarity, Spec §FR-004/FR-011]
- [x] CHK004 Are all three boundary values (exactly 0%, exactly 1%, exactly 5%) and the open intervals (>0%&<1%, >5%) each mapped to a single unambiguous alert type? [Clarity, Spec §FR-009]
- [x] CHK005 Is the discount_vs_average formula ((average − client_price)/average × 100) specified precisely, with the order of the RISK/HIGH_PRICE guards preceding it? [Consistency, Spec §FR-007]
- [x] CHK006 Is step 8 documented as a defensive/unreachable branch (degrade to HIGH_PRICE, not crash) with its unreachability rationale? [Clarity, Spec §Edge Cases, FR-007]
- [x] CHK007 Is the decision requirement stated so a single input set can only ever produce one alert type (mutually exclusive, exhaustive branches)? [Measurability, Spec §SC-001]

## Monetary / Decimal Correctness

- [x] CHK008 Is Decimal arithmetic with explicit quantization to 4 places ROUND_HALF_UP **before** any boundary comparison specified (never binary float)? [Clarity, Spec §FR-008]
- [x] CHK009 Is NUMERIC(18,4) / Decimal mandated for all monetary fields, and float prohibited? [Completeness, Spec §FR-008]
- [x] CHK010 Is rejection of NaN/Infinity at the value boundary specified? [Completeness, Spec §FR-008]
- [x] CHK011 Is the empty-competitor-set case defined to avoid divide-by-zero (short-circuits to NO_COMPETITOR_DATA before the average division)? [Edge Case, Spec §Edge Cases, FR-007]
- [x] CHK012 Is the requirement measurable that the same inputs yield byte-identical benchmarks and alert type across runs? [Measurability, Spec §SC-001]

## Currency Filtering

- [x] CHK013 Is the competitor include-predicate (success=true AND comparable=true AND currency=client_currency AND price not null) fully and unambiguously specified? [Clarity, Spec §FR-010]
- [x] CHK014 Is the currency-mismatch action specified (exclude from comparison, mark the match current price comparable=false, store CURRENCY_MISMATCH)? [Completeness, Spec §FR-010, §Edge Cases]
- [x] CHK015 Is the no-cross-currency / no-FX-in-v1 constraint stated consistently with the money section? [Consistency, Spec §FR-010]
- [x] CHK016 Is it specified that mismatched-currency competitors never affect benchmarks or the alert (SC-006), consistent with the include predicate? [Consistency, Spec §SC-006]

## Event-Transition Integrity

- [x] CHK017 Is the full CREATED/UPDATED/RESOLVED/REOPENED/UNCHANGED transition rule specified against (previous, new) type/severity, with an ordered decision? [Completeness, Spec §Clarifications, FR-013]
- [x] CHK018 Is "persist an event only when type or severity changes" stated, and the unchanged-run behavior (no event, last_seen_at advances) defined? [Clarity, Spec §FR-013, SC-004]
- [x] CHK019 Is the alert-state status vocabulary (ACTIVE while non-NORMAL, RESOLVED with resolved_at on return to NORMAL/NONE) specified? [Completeness, Spec §Clarifications, FR-013]
- [x] CHK020 Are first_seen_at / last_seen_at / resolved_at maintenance rules specified for each transition? [Completeness, Spec §FR-013]
- [x] CHK021 Is REOPENED distinguished from CREATED by the presence of a prior resolution, unambiguously? [Clarity, Spec §Clarifications]
- [x] CHK022 Is the previous/new type & severity capture on each event row specified for auditability? [Completeness, Spec §FR-003]

## Idempotency & Dedup-per-variant-per-job

- [x] CHK023 Is idempotency defined precisely (re-running with unchanged inputs → identical state AND no duplicate events)? [Measurability, Spec §FR-014, SC-001]
- [x] CHK024 Is "deduplicated per variant per job" specified as a single recompute execution per (variant, job), not per completed match? [Clarity, Spec §FR-012, SC-007]
- [x] CHK025 Is the anti-contention intent (many match completions collapse so the variant state row is not written per match) stated as a binding requirement? [Completeness, Spec §FR-012, SC-007]
- [x] CHK026 Is the dedup key / mechanism either specified or explicitly deferred to planning with the single-recompute guarantee retained? [Ambiguity, Spec §Assumptions]

## Recompute Triggers

- [x] CHK027 Are all three recompute triggers (scrape completion; client price/currency change; match archive/pause) enumerated and each routed to the same idempotent task? [Completeness, Spec §FR-015]
- [x] CHK028 Is the immediacy requirement for a client price/currency change (reflected without waiting for the next scrape) stated and measurable? [Clarity, Spec §FR-016, SC-003]
- [x] CHK029 Is the scrape-completion trigger's dedup-per-variant-per-job wiring consistent with the SPEC-07/08 completion path it depends on? [Consistency, Spec §FR-015, Assumptions]
- [x] CHK030 Is the "match archived/paused → comparable set changed → recompute" trigger specified precisely enough to test? [Clarity, Spec §FR-015]

## Workspace Isolation & Data Model

- [x] CHK031 Is workspace isolation (app scoping + Postgres RLS) required on all three new tables, with zero-context reads returning zero rows? [Completeness, Spec §FR-005, SC-008]
- [x] CHK032 Are the exact §22 column shapes + unique(workspace_id, product_variant_id) on the two state tables specified? [Completeness, Spec §FR-001/FR-002]
- [x] CHK033 Is price_alert_events specified as monthly-partitioned-from-birth with the partition key in the PK, and initial partitions created in the migration? [Completeness, Spec §FR-003, FR-006]
- [x] CHK034 Is the single-head forward migration requirement (chain onto current head, remain single-head) explicit? [Completeness, Spec §FR-006]
- [x] CHK035 Is tolerance of dangling soft references (latest_alert_state_id / observation refs into dropped partitions) specified so readers don't fail? [Edge Case, Spec §Edge Cases]

## Read Endpoints

- [x] CHK036 Is the price-comparison response contract (client price/currency, cheapest/average/highest, comparable_count, alert type/severity) fully specified? [Completeness, Spec §FR-017, SC-005]
- [x] CHK037 Are the alerts/current (list + by-variant, filters) and alert-events (paginated, variant filter) contracts specified with workspace scoping + scope gating? [Completeness, Spec §FR-018/FR-019/FR-020]
- [x] CHK038 Is 404/miss behavior for an unknown or cross-workspace variant on the read endpoints specified? [Clarity, Spec §FR-017]

## Testability & Scope

- [x] CHK039 Is exhaustive unit coverage of the pure engine (every boundary, every transition, currency filter, severity map, dedup key) required, plus skip-clean integration tests where infra is absent? [Measurability, Spec §Assumptions]
- [x] CHK040 Are out-of-scope boundaries (rollups SPEC-15, webhooks SPEC-16, access/proxies SPEC-10, locks/rate-limit SPEC-11, scheduler SPEC-13, deferred endpoints) documented to prevent scope leakage? [Consistency, Spec §Assumptions]

## Notes

- Evaluated against spec.md (with Clarifications), plan.md, research.md, data-model.md, and contracts/.
- Result: 40/40 pass. The spec + Clarifications pin the previously doc-silent items (alert-state status vocabulary, only-persist-on-change, the ordered event-transition rule); dedup key storage is deferred to planning (resolved in research.md as Redis SET NX) with the single-recompute guarantee binding. No requirement-quality gap blocks `/speckit-tasks`.
