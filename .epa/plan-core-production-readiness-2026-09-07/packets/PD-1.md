# Packet PD-1 — Phase D: Stage D — Certify and expand
Model: opus   Parallel-safe: no   Depends on: PD-3
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: D-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### D1 — Controlled-origin fleet test at 100 × 5,000 in a temporary Railway staging environment
Acceptance criteria:
- `apps/fixture-origin/` is a FastAPI app serving deterministic product pages `/p/{store}/{sku}` with per-store configurable latency, error rate, redirect, 'blocked' and 'not listed' behaviours, plus a JSON endpoint variant; 500,000 SKUs are GENERATED from the path (no data files); it makes no external calls.
- `scripts/seed_synthetic_fleet.py` creates 100 workspaces x 5,000 products x 1.185 matches pointing at `fixture-origin` URLs, with one daily refresh rule per workspace, start times jittered 14.4 min apart (audit §10).
- `scripts/fleet_test_report.py` reads the A5/A7 metrics and the D5 scorecard for a run window and emits the audit §13 gate table with pass/fail per row.
- `docs/ops/STAGING_ENVIRONMENT.md` documents create and tear-down: duplicate the production environment in Railway; `SCRAPYD_*_URLS` at 1 HTTP + 2 browser nodes; a fresh Postgres restored from the latest dump via `scripts/dr/rehearse_upgrade.sh`; proxy credentials ABSENT so no real-domain traffic can occur; the environment is deleted after D2 sign-off.
- `tests/unit/test_fixture_origin_pages.py` and `tests/unit/test_seed_synthetic_fleet_shape.py` pass.
- Steps 2, 3 and 4 are DEFERRED (OWNER creates the staging environment; the runs and `docs/ops/FLEET_TEST_2026-09.md` need it). Record the exact commands and the pass bar (>= 99% terminal within 24 h, no unbounded queue, rollup < 30 min, pool wait p95 < 100 ms) and mark them deferred. Create `docs/ops/FLEET_TEST_2026-09.md` as a skeleton with empty measurement rows.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D1: Controlled-origin fleet test at 100 × 5,000 in a temporary Railway staging environment (F22, audit §13 Headroom/Database/Tail latency) — OWNER creates the environment
>
> **Files:**
> - Create: `apps/fixture-origin/` (FastAPI app: deterministic product pages `/p/{store}/{sku}` with per-store configurable latency, error rate, redirect, "blocked" and "not listed" behaviours; a JSON endpoint variant; 500,000 SKUs generated from the path; no external calls)
> - Create: `scripts/seed_synthetic_fleet.py` (100 workspaces × 5,000 products × 1.185 matches → `fixture-origin` URLs; one daily refresh rule per workspace with start times jittered 14.4 min apart per audit §10)
> - Create: `scripts/fleet_test_report.py` (reads A5/A7 metrics and the D5 scorecard for the run window; emits the §13 gate table with pass/fail)
> - Create: `docs/ops/STAGING_ENVIRONMENT.md` (create/tear down: duplicate the production environment in Railway; `SCRAPYD_*_URLS` at 1 HTTP + 2 browser nodes; a fresh Postgres restored from the latest dump via `scripts/dr/rehearse_upgrade.sh`; proxy credentials **absent** so no real-domain traffic can occur)
> - Test: `tests/unit/test_fixture_origin_pages.py`, `tests/unit/test_seed_synthetic_fleet_shape.py`
>
> - [ ] **Step 1 (EPA):** failing tests; implement; unit green; commit `feat(test): fixture origin service, synthetic 100×5000 fleet seeder, fleet test report (F22)`.
> - [ ] **Step 2 (OWNER GATE):** create the staging environment per the doc; deploy the release-3 images plus `fixture-origin`.
> - [ ] **Step 3 (EPA, in staging):** run the seeder; run one full daily cycle; then a 2× offered-load hour (two rules per workspace). Record: completion fraction within 24 h, `crawmatic_target_phase_p95_seconds` per phase, DB pool wait, queue depth, spool pending, node loads, rollup duration for 500,000 variants (C7 benchmark), retention dry-run duration, `alembic upgrade head` duration on the restored projected-size DB, backup export bytes and time. Pass: ≥ 99% terminal within 24 h on the synthetic origin; no unbounded queue; rollup < 30 min; pool wait p95 < 100 ms.
> - [ ] **Step 4:** write `docs/ops/FLEET_TEST_2026-09.md` with the numbers and the derived node-count formula for production (audit §10 sizing hypotheses replaced by measurements).

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D1.md

## Scope
Files/areas in scope:
- **D1** — Create `apps/fixture-origin/` (app + Dockerfile + railway.json), `scripts/seed_synthetic_fleet.py`, `scripts/fleet_test_report.py`, `docs/ops/STAGING_ENVIRONMENT.md`, `docs/ops/FLEET_TEST_2026-09.md` (skeleton), `tests/unit/test_fixture_origin_pages.py`, `tests/unit/test_seed_synthetic_fleet_shape.py`.

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
- **D1** — Everything here is new. `scripts/fleet_test_report.py` reads D5's `fleet_daily_scorecard` table and the metric names A5/A7 shipped - D5 landed in the previous wave, so use its real column names. `scripts/dr/rehearse_upgrade.sh` came from A10. The 500k rollup benchmark `tests/benchmarks/test_rollup_500k.py` (C7, marker `benchmark`) is the one the plan says to run in D1 - it stays unrun here.

Task-specific notes:
- **D1** — Destructive firewall: never create, modify or delete a Railway environment, service or variable. Never run the seeder against production.
- **D1** — The seeder must refuse to run against a non-staging `DATABASE_URL` (guard on an explicit `--i-know-this-is-staging` style flag plus a host check) - it writes 100 workspaces and ~592,500 matches.

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

