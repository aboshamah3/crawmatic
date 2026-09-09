# Packet PA-7 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: sonnet   Parallel-safe: yes (with PA-1)   Depends on: P0-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A9 — Query statistics in production
Acceptance criteria:
- `scripts/analyze_hot_query_plans.py` gains `--from-pg-stat-statements --window-minutes N`: snapshot, sleep, snapshot, then report deltas ordered by total time, with calls and temp bytes.
- `tests/unit/test_query_stats_delta.py` covers delta arithmetic on two fake snapshots, top-N ordering, and a query present in the first snapshot but missing from the second (reported, not a crash).
- `docs/ops/QUERY_STATS.md` documents the report and carries the owner step verbatim (`ALTER SYSTEM SET shared_preload_libraries='pg_stat_statements'`, `pg_stat_statements.track='all'`, restart, `CREATE EXTENSION`) with a blank line for the reset time.
- Step 2 is an OWNER GATE executed in the A10 window - do NOT touch any Railway Postgres setting. Mark it deferred in the report.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A9: Query statistics in production (deep dive §7.4) — code, then OWNER step inside A10
>
> **Files:**
> - Modify: `scripts/analyze_hot_query_plans.py` (add `--from-pg-stat-statements --window-minutes N`: snapshot, sleep, snapshot, report deltas by total time, calls, temp bytes)
> - Create: `docs/ops/QUERY_STATS.md`
> - Test: `tests/unit/test_query_stats_delta.py`
>
> - [ ] **Step 1:** failing test on two fake snapshots (delta arithmetic, top-N ordering, a query missing from the second snapshot is reported, not crashed). Implement.
> - [ ] **Step 2 (owner, in A10 window):** on the Railway Postgres service: `ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'; ALTER SYSTEM SET pg_stat_statements.track = 'all';` → restart the service → `CREATE EXTENSION pg_stat_statements;` as the admin role. Record the reset time in `docs/ops/QUERY_STATS.md`.
> - [ ] **Step 3:** commit `feat(ops): pg_stat_statements delta report (deep dive §7.4)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A9.md

## Scope
Files/areas in scope:
- **A9** — Modify `scripts/analyze_hot_query_plans.py`. Create `docs/ops/QUERY_STATS.md`, `tests/unit/test_query_stats_delta.py`.

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
- **A9** — `scripts/analyze_hot_query_plans.py` is 217 lines and exists. This packet is fully file-disjoint from every other Stage A task, which is why it runs in parallel.

Task-specific notes:
- **A9** — You are running in parallel with another worker. Stay strictly inside the three files above; do not touch `config.py`, `enums.py`, `pipelines.py`, `tasks_jobs.py` or `opsmetrics/emit.py`.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. Focused (A9): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit/test_query_stats_delta.py -q`
2. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

