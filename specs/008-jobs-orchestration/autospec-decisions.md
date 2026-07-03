# Autospec Decisions — SPEC-08 Jobs & Orchestration

Log of auto-answered questions and informed defaults. Format:
`- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Which run endpoints are in scope for this spec? → A: match- and variant-scoped only (`POST /v1/jobs/run/match/{id}`, `POST /v1/jobs/run/variant/{id}`); product/group/competitor/workspace runs + scheduler are later specs (source: doc §35 "08 — Jobs & Orchestration" acceptance criteria + §36 order — scheduler is spec 13).
- [specify] Q: Does this spec include price analysis / current-price update / alerting? → A: No — ends at persisting job/target outcomes; price_analysis + alert logic are SPEC-09 (source: doc §35 "09 — Current Prices & Alert Logic").
- [specify] Q: How are node selection and duplicate dispatch handled? → A: deterministic node selection (hash by domain or persisted-node round-robin) + idempotent dispatch guard keyed on job/batch; stalled-batch detection re-dispatches after timeout under the same guards (source: doc §26 Celery Queues → scrape_dispatch "Node handling within each Scrapyd pool").
- [specify] Q: How are job counters maintained? → A: aggregated from scrape_job_targets periodically and at finalization, never incremented per-target (source: doc §26 price_analysis note + §35 covers "job counters aggregated from targets").
- [specify] Q: How is Celery fork-safety handled? → A: dispose inherited engine on worker_process_init before first use (source: doc §4/§35 "Celery engine fork-safety"; §2 principle on fork-safety).
- [specify] Q: What HTTP batch size for grouping? → A: 50–200 matches per HTTP batch, grouped by workspace/competitor-domain/scrape-mode; never one Scrapyd job per URL (source: doc §27 Batching Strategy).
- [specify] Q: How is a "batch" represented (table vs derived)? → A: left to planning; binding requirements are deterministic-node + idempotency guarantees, not a specific schema (default — spec §Assumptions).
- [specify] Q: Live infra verification approach? → A: unit tests + integration tests that skip cleanly (no Docker/Postgres/Redis/Scrapyd in build env), consistent with SPEC-01…07 (source: project convention + prior state file deferred_verifications).

## clarify

- [clarify] Q: What type/source for direct API run endpoints? → A: type=MANUAL, source=API (operator on-demand); API_TRIGGERED/PLUGIN/SCHEDULED reserved for programmatic/scheduler triggers (default — doc §22 lists the enum values but not the mapping; informed guess, integrated into spec FR-010).
- [clarify] Q: How does a zero-active-match scoped run resolve? → A: create job, total_targets=0, no dispatch, finalize COMPLETED (default — doc §25 silent on empty case; chosen for observability + idempotency, integrated into US2-AS4 + FR-020).
- [clarify] Q: Stall-timeout value + idempotency-guard storage (Redis vs DB)? → A: deferred to planning (config/implementation); binding reqs are detect+re-dispatch past configured timeout and no-double-run (source: doc §26 states the behavior, not the constant).
- [clarify] No questions relayed to user — all ambiguities resolved doc-first / by informed default with clear reasonable defaults; none high-impact enough to require human decision.

## analyze

speckit-analyze: 0 CRITICAL, 0 HIGH, 3 MEDIUM, 5 LOW. No user pause required (no CRITICAL). Remediations applied to artifacts (analyze is read-only; orchestrator made the edits):

- [analyze] I1 (MEDIUM, browser routing): dispatch/recover routed EVERY batch to SCRAPYD_HTTP_URLS, so a BROWSER-mode batch would hit HTTP nodes. → Fixed: node pool chosen by batch.mode (BROWSER→SCRAPYD_BROWSER_URLS else SCRAPYD_HTTP_URLS) in T026, T040, dispatch-task.md, node-selection.md, stall-recovery.md; spec Assumptions note added (browser spider/service itself is SPEC-14).
- [analyze] U1 (MEDIUM, target terminalization owner): mark_target had no production caller → counters/finalization only test-simulated. → Fixed: added T052 wiring the SPEC-07 pipeline `_flush_batch` to call mark_target per item in-transaction + enqueue event-driven finalize per affected job; added T053 unit test; FR-017 reworded to name the caller; lifecycle-counters.md updated. Makes FR-017/018/019 operational without the SPEC-13 beat.
- [analyze] B1 (MEDIUM, fencing-token lock deferred to SPEC-11): documented cross-spec boundary, not a silent violation. → No code change; added explicit Assumptions note in spec (unique constraint + idempotency guard + target-state checks carry re-dispatch safety until SPEC-11 lands the lock).
- [analyze] A1 (LOW, skipped-only job → FAILED was semantically surprising): → Fixed: finalization rule made failure-centric (no-failures ⇒ COMPLETED incl. skipped-only; PARTIAL_FAILED requires ≥1 failure AND ≥1 success; FAILED requires ≥1 failure AND 0 success). Updated FR-019, T037, T041, lifecycle-counters.md, US3 wording.
- [analyze] U3 (LOW, recover_stalled missing domain/mode re-resolution): → Fixed: T040 + stall-recovery.md now note the set-based re-resolution.
- [analyze] C1/C2/U2/I2 (LOW, informational): documented as intentional boundaries — periodic recover needs SPEC-13 beat (event-driven finalize does not); forward-looking enum values (spec Assumptions); priority reuses MatchPriority; FR-011 workspace grouping implicit per single-workspace job. Notes added to spec Assumptions; no code change.
- [analyze] Not re-running analyze: no CRITICAL/HIGH was fixed (autospec re-run trigger not met); MEDIUM/LOW remediations self-verified for artifact consistency.
- [analyze] Task count 51 → 53 (added T052/T053); coverage table updated (FR-017/018/019/SC-007 now include T052/T053).

## converge

- [converge] speckit-converge verdict: CONVERGED — no remaining work; tasks.md left unchanged (no convergence phase appended). Verified in-code (not just [X] marks): both models + migration + RLS, all 4 scope-gated endpoints, idempotent mode-aware dispatch, aggregated failure-centric finalization, stall recovery, fork-safety, pipeline target-terminalization seam. Only unchecked items are the deliberately-deferred skip-clean live integration tests (T044–T048).
- [converge] Final gate: 1026 unit passed; integration 3 passed/148 skipped/0 errors; single head a6b0234cd4ad; scoping guard clean.
