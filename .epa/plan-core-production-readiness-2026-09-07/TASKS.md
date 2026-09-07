# Tasks: plan-core-production-readiness-2026-09-07

| ID | Phase | Packet | Task | Depends on | Model | Status | Attempts | Report |
|----|-------|--------|------|-----------|-------|--------|----------|--------|
| 0.1 | 0 | P0-1 | Backup inventory for selective disk relief (F21) | - | sonnet | running | 1 | - |
| 0.2 | 0 | P0-1 | Fast-forward engine `main` to `7a26c54` — OWNER GATE | - | sonnet | running | 1 | - |
| 0.3 | 0 | P0-2 | Release identity manifest and configuration diff (F20 part 1) | - | opus | pending | 0 | - |
| 0.4 | 0 | P0-2 | Isolated unit-test environment (audit §2) | - | opus | pending | 0 | - |
| A1 | A | PA-1 | Browser egress enforced at connection time (F01, P0) | 0.4 | opus | pending | 0 | - |
| A2 | A | PA-2 | Regex execution with a hard timeout (F02) | 0.4 | opus | pending | 0 | - |
| A3 | A | PA-3 | Component-scoped credentials and grants (F03) | 0.4 | sonnet | pending | 0 | - |
| A4 | A | PA-3 | Advisory triage gate and pinned build tools (F04) | A1 | sonnet | pending | 0 | - |
| A5 | A | PA-4 | Target lifecycle truth — STARTED transition, phase timestamps, baseline metrics | A3 | opus | pending | 0 | - |
| A6 | A | PA-5 | Canary calculator counts pages, prices and provider dimension | 0.4 | sonnet | pending | 0 | - |
| A7 | A | PA-6 | Ledger coverage and bytes at the transport boundary | 0.4 | sonnet | pending | 0 | - |
| A8 | A | PA-5 | Cost artifacts and watchdog corrected | A2 | sonnet | pending | 0 | - |
| A9 | A | PA-7 | Query statistics in production | 0.4 | sonnet | pending | 0 | - |
| A10 | A | PA-8 | Engine release 1 — OWNER GATE (F20) | A1-A9 | sonnet | pending | 0 | - |
| B1 | B | PB-1 | Durable result spool with idempotent persistence and backpressure (F05) | A5, A10 | opus | pending | 0 | - |
| B2 | B | PB-2 | Dispatch intent committed before the POST; stable remote job id; outbox (F06) | B1 | opus | pending | 0 | - |
| B3 | B | PB-3 | Atomic due-time claim, unique occurrences, per-rule isolation, fair mode on (F07) | B2, B4 | opus | pending | 0 | - |
| B4 | B | PB-4 | Two Celery consumer pools; time limits; resumable long maintenance (F09) | B2 | sonnet | pending | 0 | - |
| B5 | B | PB-5 | Fleet-wide host admission at the physical request boundary (F10) | B3 | opus | pending | 0 | - |
| B6 | B | PB-6 | Capacity-aware placement persisted on the intent; bounded node queues (F11) | B2, B4 | opus | pending | 0 | - |
| B7 | B | PB-7 | Non-blocking API middleware with deadlines everywhere (F15) | B1 | sonnet | pending | 0 | - |
| B8 | B | PB-8 | Readiness probes that cannot pile up; liveness/dependency/scraping split (F16) | A10 | sonnet | pending | 0 | - |
| B9 | B | PB-8 | Heartbeats for every process class and alerts to an owner (F22) | A5, A7 | sonnet | pending | 0 | - |
| B10 | B | PB-9 | Engine release 2 — OWNER GATE | B1-B9 | sonnet | pending | 0 | - |
| C1 | C | PC-1 | Per-target deadline, physical attempt budget, failure classes, method suppression (F08) | B5, B10 | opus | pending | 0 | - |
| C2 | C | PC-2 | Amazon HTTP-leg investigation with the resolved production profile — spend ≤ $1 | C1 | sonnet | pending | 0 | - |
| C3 | C | PC-2 | Document-only browser canary with the fixed calculator — spend ≤ $3 | A6, C1 | sonnet | pending | 0 | - |
| C4 | C | PC-3 | Noon and S-Tech labeled canaries; versioned domain strategy; safe coalescing key — spend ≤ $2 | C1 | opus | pending | 0 | - |
| C5 | C | PC-4 | Structured offer contract on the live path; ranker shadow; durable evidence (F19) | C4 | opus | pending | 0 | - |
| C6 | C | PC-5 | Usage aggregation by provider dimension; reservation by known first rung (F17, F18) | B10 | opus | pending | 0 | - |
| C7 | C | PC-6 | Set-based daily rollups with keyset batches and checkpoints (F12) | C5 | opus | pending | 0 | - |
| C8 | C | PC-7 | Retention gated on per-key coverage and the completion watermark (F13) | C7 | sonnet | pending | 0 | - |
| C9 | C | PC-8 | Retention per data class; ledger child summarization; partitioning (F14) | C8 | opus | pending | 0 | - |
| C10 | C | PC-9 | Backups from inside Railway, measured, encrypted, off-host (F21) | B10 | opus | pending | 0 | - |
| C11 | C | PC-10 | Engine release 3 — OWNER GATE | C1-C10 | sonnet | pending | 0 | - |
| D1 | D | PD-1 | Controlled-origin fleet test at 100 × 5,000 in a temporary Railway staging environment | D5 | opus | pending | 0 | - |
| D2 | D | PD-2 | Fault-injection matrix in staging | C11 | sonnet | pending | 0 | - |
| D3 | D | PD-2 | Bounded real-domain canary with provider and container reconciliation — spend ≤ $3 | C11 | sonnet | pending | 0 | - |
| D4 | D | PD-4 | Staged rollout 1 → 5 → 20 → 100 stores with stop rules — OWNER GATE per step | D1, D2, D3 | sonnet | pending | 0 | - |
| D5 | D | PD-3 | Daily cost and freshness scorecard | C11 | sonnet | pending | 0 | - |
| D6 | D | PD-4 | Re-score against the gate table and close the plan | D1-D5 | sonnet | pending | 0 | - |
