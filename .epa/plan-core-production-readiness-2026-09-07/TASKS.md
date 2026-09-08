# Tasks: plan-core-production-readiness-2026-09-07

| ID | Phase | Packet | Task | Depends on | Model | Status | Attempts | Report |
|----|-------|--------|------|-----------|-------|--------|----------|--------|
| 0.1 | 0 | P0-1 | Backup inventory for selective disk relief (F21) | - | sonnet | done-dev | 1 | reports/0.1.md |
| 0.2 | 0 | P0-1 | Fast-forward engine `main` to `7a26c54` — OWNER GATE | - | sonnet | done-dev | 1 | reports/0.2.md |
| 0.3 | 0 | P0-2 | Release identity manifest and configuration diff (F20 part 1) | - | opus | done-dev | 1 | reports/0.3.md |
| 0.4 | 0 | P0-2 | Isolated unit-test environment (audit §2) | - | opus | done-dev | 1 | reports/0.4.md |
| A1 | A | PA-1 | Browser egress enforced at connection time (F01, P0) | 0.4 | opus | done-dev | 1 | reports/A1.md |
| A2 | A | PA-2 | Regex execution with a hard timeout (F02) | 0.4 | opus | done-dev | 1 | reports/A2.md |
| A3 | A | PA-3 | Component-scoped credentials and grants (F03) | 0.4 | sonnet | done-dev (MATERIAL: grants kept, owner decision at A10) | 1 | reports/A3.md |
| A4 | A | PA-3 | Advisory triage gate and pinned build tools (F04) | A1 | sonnet | done | 1 | reports/A4.md |
| A5 | A | PA-4 | Target lifecycle truth — STARTED transition, phase timestamps, baseline metrics | A3 | opus | done-dev | 1 | reports/A5.md |
| A6 | A | PA-5 | Canary calculator counts pages, prices and provider dimension | 0.4 | sonnet | done-dev | 1 | reports/A6.md |
| A7 | A | PA-6 | Ledger coverage and bytes at the transport boundary | 0.4 | sonnet | done-dev | 1 | reports/A7.md |
| A8 | A | PA-5 | Cost artifacts and watchdog corrected | A2 | sonnet | done-dev | 1 | reports/A8.md |
| A9 | A | PA-7 | Query statistics in production | 0.4 | sonnet | done-dev | 1 | reports/A9.md |
| A10 | A | PA-8 | Engine release 1 — OWNER GATE (F20) | A1-A9 | sonnet | done-dev (step 3 owner, deferred) | 1 | reports/A10.md |
| B1 | B | PB-1 | Durable result spool with idempotent persistence and backpressure (F05) | A5, A10 | opus | done-dev (integration deferred) | 1 | reports/B1.md |
| B2 | B | PB-2 | Dispatch intent committed before the POST; stable remote job id; outbox (F06) | B1 | opus | done-dev (+B2-fix1: RLS backfill, grants) | 1 | reports/B2.md, reports/B2-fix1.md |
| B3 | B | PB-3 | Atomic due-time claim, unique occurrences, per-rule isolation, fair mode on (F07) | B2, B4 | opus | done-dev | 1 | reports/B3.md |
| B4 | B | PB-4 | Two Celery consumer pools; time limits; resumable long maintenance (F09) | B2 | sonnet | done-dev | 1 | reports/B4.md |
| B5 | B | PB-5 | Fleet-wide host admission at the physical request boundary (F10) | B3 | opus | done-dev | 1 | reports/B5.md |
| B6 | B | PB-6 | Capacity-aware placement persisted on the intent; bounded node queues (F11) | B2, B4 | opus | done-dev | 1 | reports/B6.md |
| B7 | B | PB-7 | Non-blocking API middleware with deadlines everywhere (F15) | B1 | sonnet | done-dev (abuse_limit kept, see ASSUMPTIONS) | 1 | reports/B7.md |
| B8 | B | PB-8 | Readiness probes that cannot pile up; liveness/dependency/scraping split (F16) | A10 | sonnet | done-dev | 1 | reports/B8.md |
| B9 | B | PB-8 | Heartbeats for every process class and alerts to an owner (F22) | A5, A7 | sonnet | done-dev (+B9-fix1 wired worker+scheduler emitters; rules gauge wiring follow-up) | 1 | reports/B9.md, reports/B9-fix1.md |
| B10 | B | PB-9 | Engine release 2 — OWNER GATE | B1-B9 | sonnet | done (owner gate: release 2 deploy deferred to owner) | 1 | reports/B10.md |
| C1 | C | PC-1 | Per-target deadline, physical attempt budget, failure classes, method suppression (F08) | B5, B10 | opus | done-dev (budget gate inert until C4 passes budget=) | 1 | reports/C1.md |
| C2 | C | PC-2 | Amazon HTTP-leg investigation with the resolved production profile — spend ≤ $1 | C1 | sonnet | done-dev (spend step C2.2 owner-run) | 1 | reports/C2.md |
| C3 | C | PC-2 | Document-only browser canary with the fixed calculator — spend ≤ $3 | A6, C1 | sonnet | done-dev (spend step C3.2 owner-run) | 1 | reports/C2.md |
| C4 | C | PC-3 | Noon and S-Tech labeled canaries; versioned domain strategy; safe coalescing key — spend ≤ $2 | C1 | opus | done-dev (canary spend step C4.2 owner-run) | 1 | reports/C4.md |
| C5 | C | PC-4 | Structured offer contract on the live path; ranker shadow; durable evidence (F19) | C4 | opus | done-dev (incl. spider raw_evidence continuation) | 1 | reports/C5.md |
| C6 | C | PC-5 | Usage aggregation by provider dimension; reservation by known first rung (F17, F18) | B10 | opus | done (attempt 2) | 2 | reports/C6.md, reports/C6-2.md |
| C7 | C | PC-6 | Set-based daily rollups with keyset batches and checkpoints (F12) | C5 | opus | done-dev (500k benchmark written, not run → D1) | 1 | reports/C7.md |
| C8 | C | PC-7 | Retention gated on per-key coverage and the completion watermark (F13) | C7 | sonnet | done | 1 | reports/C8.md |
| C9 | C | PC-8 | Retention per data class; ledger child summarization; partitioning (F14) | C8 | opus | done-dev (owner ratification: FK trade + RETENTION_ENABLED_CLASSES) | 1 | reports/C9.md |
| C10 | C | PC-9 | Backups from inside Railway, measured, encrypted, off-host (F21) | B10 | opus | done-dev (owner gate step 3 deferred; backup-report route has no receiver) | 1 | reports/C10.md |
| C11 | C | PC-10 | Engine release 3 — OWNER GATE | C1-C10 | sonnet | done (owner gate: release 3 deploy deferred) | 1 | reports/C11.md |
| D1 | D | PD-1 | Controlled-origin fleet test at 100 × 5,000 in a temporary Railway staging environment | D5 | opus | done-dev (steps 2–4 owner-deferred; 500k benchmark PASS 2.3 s) | 1 | reports/D1.md |
| D2 | D | PD-2 | Fault-injection matrix in staging | C11 | sonnet | done-dev (staging runs owner-deferred) | 1 | reports/D2.md |
| D3 | D | PD-2 | Bounded real-domain canary with provider and container reconciliation — spend ≤ $3 | C11 | sonnet | done-dev (canary spend ≤$3 owner-deferred) | 1 | reports/D2.md |
| D4 | D | PD-4 | Staged rollout 1 → 5 → 20 → 100 stores with stop rules — OWNER GATE per step | D1, D2, D3 | sonnet | done-dev (rollout execution owner-deferred) | 1 | reports/D4.md |
| D5 | D | PD-3 | Daily cost and freshness scorecard | C11 | sonnet | done-dev (Railway CPU/RAM/egress columns NULL until watchdog store) | 1 | reports/D5.md |
| D6 | D | PD-4 | Re-score against the gate table and close the plan | D1-D5 | sonnet | done-dev (re-score: 0 PASS, 5 PARTIAL, 7 NOT YET MEASURED of 12) | 1 | reports/D4.md |
