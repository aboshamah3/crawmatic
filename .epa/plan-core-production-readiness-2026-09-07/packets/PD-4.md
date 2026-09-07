# Packet PD-4 — Phase D: Stage D — Certify and expand
Model: sonnet   Parallel-safe: no   Depends on: PD-1, PD-2, PD-3
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: D-w3   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### D4 — Staged rollout 1 → 5 → 20 → 100 stores with stop rules — OWNER GATE per step
Acceptance criteria:
- `docs/ops/ROLLOUT_2026-09.md` describes the 1 -> 5 -> 20 -> 100 store rollout: at each step the owner enables refresh rules for N stores (jittered), waits seven daily cycles and reads the D5 scorecard.
- The ADVANCE bar is stated verbatim and completely: terminal >= 99% within 24 h; fresh comparable >= 95%; attempts per valid fresh <= 1.5; cost per valid refresh <= the C11-approved bound; no B9 alert unresolved > 24 h.
- The STOP rule is stated verbatim: any day below 95% terminal, any persistence quarantine, any budget denial, or a freshness alert -> hold at the current N and open an incident per `docs/INCIDENT_RESPONSE.md`.
- The doc records that the staging environment is deleted after D2 sign-off, before step 1, and carries a per-step gate table for the owner to fill.
- Step 2 is an OWNER GATE per step: prepare only, mark deferred.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D4: Staged rollout 1 → 5 → 20 → 100 stores with stop rules — OWNER GATE per step
>
> **Files:**
> - Create: `docs/ops/ROLLOUT_2026-09.md`
>
> - [ ] **Step 1 (EPA):** write the rollout doc: at each step the owner enables refresh rules for N stores (jittered), waits seven daily cycles, and reads the D5 scorecard; **advance** only if all of: terminal ≥ 99% within 24 h; fresh comparable ≥ 95%; attempts per valid fresh ≤ 1.5; cost per valid refresh ≤ the C11-approved bound; no B9 alert unresolved > 24 h. **Stop rule**: any day below 95% terminal, any persistence quarantine, any budget denial, or a freshness alert → hold at the current N and open an incident per `docs/INCIDENT_RESPONSE.md`. The staging environment is deleted after D2 sign-off, before step 1.
> - [ ] **Step 2 (owner):** execute the steps; record each gate in the doc.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D4.md

### D6 — Re-score against the gate table and close the plan
Acceptance criteria:
- `docs/PRODUCTION_READINESS_SCORE_2026-09.md` gains a '2026-09 post-Stage-D re-score' section reproducing the audit §13 gate table, one row per gate, each with an EVIDENCE LINK: a test file path, a doc path, a scorecard day, or a job id.
- Rows whose evidence depends on a deferred owner gate, a deferred staging run or a deferred spend step are marked DEFERRED with the exact blocking gate named - they are NOT marked PASS, and the section states plainly that the plan is not complete while any row is not PASS.
- `docs/DEFERRED-ITEMS.md` closes the three items this plan resolves - fair queue flag (B3), proxy bytes exclude browser children (A7/C6), Amazon HTTP-leg research (C2) - and adds every new deferred item this run produced.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D6: Re-score against the gate table and close the plan
>
> **Files:**
> - Modify: `docs/PRODUCTION_READINESS_SCORE_2026-09.md` (append "2026-09 post-Stage-D re-score": the audit §13 table with an evidence link per row — test file, doc, scorecard day, or job id)
> - Modify: `docs/DEFERRED-ITEMS.md` (close the three items this plan resolves: fair queue flag, proxy bytes exclude browser children, Amazon HTTP-leg research; add anything D1–D4 surfaced)
>
> - [ ] **Step 1:** fill the table; every row is PASS with evidence or the plan is not complete. Commit `docs(readiness): 2026-09 re-score with gate evidence`.
>
> ---

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D6.md

## Scope
Files/areas in scope:
- **D4** — Create `docs/ops/ROLLOUT_2026-09.md`. Modify nothing else.
- **D6** — Modify `docs/PRODUCTION_READINESS_SCORE_2026-09.md`, `docs/DEFERRED-ITEMS.md`. Modify nothing else.

Conventions:
- Repo root `/srv/crawmatic/crawmatic`, branch `epa/plan-core-production-readiness-2026-09-07`. Run every engine command as `mahmoud`: `sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/pytest ...`.
- The interpreter in `.venv` is **Python 3.13.14** even though the plan text says 3.12. Do not "fix" that.
- Definition of done per the plan: failing test first, implementation, tests green.
- **Do NOT run `git commit`.** The plan's "Commit `...`" steps are for the orchestrator, which commits per phase after review. Leave your changes uncommitted in the main tree (worker contract rule 8) and put the intended commit message in your report. Never `git add -A`.
- **Core only.** Nothing under `/srv/crawmatic/saas` may be modified.
- One Alembic head at all times; every new revision chains from the current head. Partitioned tables never get a bulk `DELETE`; drops are whole partitions.
- Money is micro-USD (1 USD = 1,000,000). The proxy billing unit is a named setting, never an implicit constant.
- Every read/write is workspace-scoped unless it is a registered system sweep (`_system_session()`); new fleet tables without a `workspace_id` are never exposed to tenant roles.
- Secrets: variable NAMES and file locations only, never values. Never run `env`/`printenv`/`set`.

Context (from CONTEXT.md ## Areas):
- **D4** — Owner decision 10 fixes the gate thresholds: 7 consecutive daily cycles; >= 99% of eligible matches reach a valid terminal outcome within 24 h; >= 95% obtain a fresh comparable price; not-listed and permanently unavailable listings excluded explicitly. Use those numbers, do not restate them loosely.
- **D6** — Read every report under the run dir's `reports/` plus `BLOCKERS.md` and the ASSUMPTIONS.md owner-gate ledger to fill the evidence column honestly. Per ASSUMPTIONS.md answer 2 the run can end at best `COMPLETE-WITH-DEFERRED-GATES`, so a fully PASS table is not an achievable outcome here.

Task-specific notes:
- **D4** — Documentation only. Change no code, enable no refresh rule, touch no Railway variable.
- **D6** — Do not mark a gate PASS on the strength of a written test that was never run, or on a deferred owner action. Under-claiming is correct here.
- **D6** — Documentation only.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

