# Breakdown: plan-core-production-readiness-2026-09-07

Shape **Structured** (plan phases and task IDs 0.1–D6 adopted verbatim) · Mode **direct** · Branch `epa/plan-core-production-readiness-2026-09-07` (base `7a26c54`)
**5 phases · 41 tasks · 33 packets · 23 waves (2+5+6+7+3) · max 3 agents.** Phases run strictly in plan order 0 → A → B → C → D.

## Serialization rules applied
- One Alembic-revision packet per wave (single head): A2, A3, A5 · B1, B2, B3, B4, B5 · C1, C4, C5(×2), C7, C9 · D5. **B4 is migration-bearing** — its discovery cursor needs a `strategy_discovery_state` table the plan's file list omits.
- Never concurrent: packets touching `config.py`, `enums.py`, `pipelines.py`, `tasks_jobs.py`, `opsmetrics/emit.py`, `scheduler_app.py`, `apps/api/app/main.py`, `docs/ops/CAPACITY.md`, `docs/RETENTION_POLICY.md`, `apps/scrapers-browser/Dockerfile`, `strategy/methods.py`+`resolution.py`, unless one packet owns both tasks.
- Integration/docker verification is serialized: one shared compose Postgres/Redis. Packets say "skip if a parallel sibling is running, note in report". ENOSPC → BLOCKER + deferred (disk at 97%).
- Markers registered where first needed: `browser`→A1, `load`→B7, `benchmark`→C7 (append to `[tool.pytest.ini_options] markers`, which today lists only `integration`).

## Phase 0 — Preconditions (4 tasks, 2 packets)
| Wave | Packet | Tasks | Model | Par |
|---|---|---|---|---|
| 0-w1 | P0-1 | 0.1 dr inventory (real read-only run + `--docker`), 0.2 main fast-forward **gate** | sonnet | no |
| 0-w2 | P0-2 | 0.3 release manifest + config diff, 0.4 isolated unit-test env | opus | no |
P0-2 runs alone: 0.4 rewrites `tests/conftest.py`, the file every later verification depends on.

## Stage A — Contain risk, trustworthy baseline (10 tasks, 8 packets)
| Wave | Packets (parallel) | Tasks | Models |
|---|---|---|---|
| A-w1 | PA-1 ‖ PA-7 | A1 egress guard (P0) ‖ A9 query stats | opus ‖ sonnet |
| A-w2 | PA-2 ‖ PA-6 | A2 regex deadline *(mig)* ‖ A7 wire bytes | opus ‖ sonnet |
| A-w3 | PA-3 ‖ PA-5 | A3 grants *(mig)* + A4 advisory gate ‖ A6 canary calc + A8 cost artifacts | sonnet ‖ sonnet |
| A-w4 | PA-4 | A5 lifecycle truth *(mig)* | opus |
| A-w5 | PA-8 | A10 release 1 **gate** | sonnet |
A4 is sequenced after A1 (both edit `apps/scrapers-browser/Dockerfile`). A7 precedes A5 on `opsmetrics/emit.py`.

## Stage B — Durable work, reliable schedules (10 tasks, 9 packets)
| Wave | Packets (parallel) | Tasks | Models |
|---|---|---|---|
| B-w1 | PB-1 ‖ PB-8 | B1 result spool *(mig)* ‖ B8 readiness + B9 heartbeats | opus ‖ sonnet |
| B-w2 | PB-2 ‖ PB-7 | B2 dispatch intents *(mig)* ‖ B7 async API deadlines | opus ‖ sonnet |
| B-w3 | PB-4 | B4 Celery pools + chunked discovery *(mig)* | sonnet |
| B-w4 | PB-3 | B3 atomic due-time claim, fair mode ON *(mig)* | opus |
| B-w5 | PB-5 ‖ PB-6 | B5 fleet admission *(mig)* ‖ B6 node placement | opus ‖ opus |
| B-w6 | PB-9 | B10 release 2 **gate** | sonnet |
B8+B9 share one packet because both mount routers in `apps/api/app/main.py`. B6 follows B4 (`CAPACITY.md`) and B2 (`intent.node_url`).

## Stage C — Efficient and correct results/cost (11 tasks, 10 packets)
| Wave | Packets (parallel) | Tasks | Models |
|---|---|---|---|
| C-w1 | PC-1 ‖ PC-9 | C1 attempt budget/deadlines *(mig)* ‖ C10 off-host backups | opus ‖ opus |
| C-w2 | PC-2 ‖ PC-3 ‖ PC-5 | C2+C3 canary scripts *(spend deferred)* ‖ C4 playbooks *(mig)* ‖ C6 usage/reservation | sonnet ‖ opus ‖ opus |
| C-w3 | PC-4 | C5 offer contract *(2 migs)* | opus |
| C-w4 | PC-6 | C7 set-based rollups *(mig)* | opus |
| C-w5 | PC-7 | C8 retention coverage gate | sonnet |
| C-w6 | PC-8 | C9 per-class retention + partition swap *(mig)* | opus |
| C-w7 | PC-10 | C11 release 3 **gate** | sonnet |
C1 precedes C4 (both edit the escalation ladder) and C2 (imports C1's error classes). C7→C8→C9 is the plan's own chain.

## Stage D — Certify and expand (6 tasks, 4 packets)
| Wave | Packets (parallel) | Tasks | Models |
|---|---|---|---|
| D-w1 | PD-2 ‖ PD-3 | D2 fault-injection scripts + D3 reconciliation *(runs deferred)* ‖ D5 scorecard *(mig)* | sonnet ‖ sonnet |
| D-w2 | PD-1 | D1 fixture origin + fleet seeder *(steps 2–4 deferred)* | opus |
| D-w3 | PD-4 | D4 rollout doc **gate** + D6 re-score/close | sonnet |

## Stale plan paths corrected in the packets (CONTEXT.md unknowns)
1. **B5** `models/domain_rules.py` → **create** the module and a new fleet-scoped `domain_rules` table (distinct from tenant `DomainAccessRule` and from `domain_playbooks`). C1 later adds `request_timeout_seconds` to it.
2. **B5** `scrape_core/middlewares/limiter.py` → does not exist; use the real seam `scrape_core/limiter.py` and its only call site `scrape_core/targets.py:1484`. **A7** creates the new `middlewares/` package for `wire_bytes.py` only.
3. **C1/C4** `scrape_core/strategy/escalation.py` → does not exist; the ladder is `app_shared/strategy/methods.py` + `resolution.py`.
4. **B4** `app_shared/strategy/discovery.py` → does not exist; the discovery task is `apps/workers/app/workers/tasks_strategy.py:1021` (`STRATEGY_DISCOVERY_RUN`).
5. **B9/D5** `apps/api/app/routers/admin_ops.py` → **create** it (B9) behind the same service-token guard as `ops_metrics.py`; D5 adds `/admin/scorecard` to it.
Also: A2's "existing admin router" is `apps/api/app/routers/admin.py`; Postgres major for A10/B10/C10/C11 is read from the newest dump header (compose is 17.5, plan text says 18) and logged.

## Deferred by Pre-Flight (no worker executes these)
Owner gates 0.1 step 6, 0.2, A9 step 2, A10 steps 2–3, B10 step 2, C10 step 3, C11 step 2, D1 steps 2–4, D2 runs, D4 step 2 · spend steps C2.2, C3.2, C4.2, D3.1 ($0 total) · staging-dependent runs. Packets prepare artifacts + exact commands and mark each gate deferred. Best terminal status: **COMPLETE-WITH-DEFERRED-GATES**.

## Decisions that would trigger Hard Stop 4
1. **B5** — creating a new `domain_rules` table vs. extending `domain_playbooks`/`domain_access_rules` (data model, used by C1). The packet chooses *create*; a worker that disagrees must ESCALATE.
2. **B3** — `SCHEDULER_FAIR_QUEUE_ENABLED` default flips to `True` (fleet-wide scheduling behaviour). Plan-mandated, flagged for the reviewer.
3. **C9** — the `network_operations` partition swap (data migration on a high-volume family) and `RETENTION_ENABLED_CLASSES` shipping empty pending owner ratification.
4. **C5/C11** — `EXTRACTION_RANKING_POLICY` stays `shadow`; any move to `v1` is owner-only and its evidence (C4 canaries) will not exist.
5. **C3/C11** — adding `amazon.sa` to `BROWSER_DOCUMENT_ONLY_DOMAINS` is owner-only and blocked on a deferred canary.
6. **B4** — adding an Alembic revision the plan's file list omits (discovery cursor table). If a worker instead persists the cursor in Redis, that changes B4's durability contract.
7. Any task discovering that a plan-cited file cannot be reconciled with the tree beyond the five corrections above.
