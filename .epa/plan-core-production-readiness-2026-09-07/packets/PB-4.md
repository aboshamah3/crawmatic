# Packet PB-4 — Phase B: Stage B — Durable work, reliable schedules
Model: sonnet   Parallel-safe: no   Depends on: PB-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w3   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B4 — Two Celery consumer pools; time limits; resumable long maintenance (F09)
Acceptance criteria:
- `apps/workers/start.sh` renders exactly two `celery ... worker` commands - `-Q scrape_dispatch,maintenance -c $CELERY_CRITICAL_CONCURRENCY -n critical@%h` and `-Q price_analysis,strategy_discovery,webhook_events -c $CELERY_BULK_CONCURRENCY -n bulk@%h` - and exits non-zero if either child exits (`wait -n`, then kill the other). `apps/workers/Dockerfile:30` uses `CMD ["/app/start.sh"]`.
- `tests/unit/test_worker_start_script.py` runs `bash -n` on the script and asserts the rendered commands. `tests/unit/test_celery_time_limits.py` asserts EVERY registered task in `app.tasks` has a `time_limit` below `CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS` (3600).
- Per-task limits annotated in `celery_app.py`: dispatch 600 s, reapers/reconcilers 300 s, breaker 120 s, rollups 1,800 s per chunk, discovery 900 s per chunk, analysis 900 s, webhooks 120 s. `task_queues` is unchanged.
- New settings `CELERY_CRITICAL_CONCURRENCY: int = 2`, `CELERY_BULK_CONCURRENCY: int = 2`.
- Discovery is chunked by domain with `STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN = 20` and a persisted cursor, re-enqueuing until complete (`tests/unit/test_discovery_chunk_cursor.py`).
- `docs/ops/CAPACITY.md` records the worker RAM budget (`2 pools x concurrency x ~150 MB`) and the DB connection demand formula from F20 (`api threads + worker procs + scheduler + scrapers <= PgBouncer pool`).

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B4: Two Celery consumer pools; time limits; resumable long maintenance (F09) — *parallel-safe*
>
> **Files:**
> - Create: `apps/workers/start.sh` (Dockerfile `CMD ["/app/start.sh"]`)
> - Modify: `apps/workers/Dockerfile:30`
> - Modify: `apps/workers/app/workers/celery_app.py:130-240` (per-task `soft_time_limit`/`time_limit` annotations; `task_queues` unchanged)
> - Modify: `libs/shared/app_shared/config.py` (`CELERY_CRITICAL_CONCURRENCY: int = 2`, `CELERY_BULK_CONCURRENCY: int = 2`)
> - Modify: `libs/shared/app_shared/strategy/discovery.py` (chunk by domain: `STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN = 20`, cursor persisted in a `strategy_discovery_state` row)
> - Create: `docs/ops/CAPACITY.md`
> - Test: `tests/unit/test_worker_start_script.py` (bash `-n` + rendered command assertions), `tests/unit/test_celery_time_limits.py`, `tests/unit/test_discovery_chunk_cursor.py`
>
> - [ ] **Step 1: Failing tests**: `start.sh` renders exactly two `celery ... worker` commands: `-Q scrape_dispatch,maintenance -c $CELERY_CRITICAL_CONCURRENCY -n critical@%h` and `-Q price_analysis,strategy_discovery,webhook_events -c $CELERY_BULK_CONCURRENCY -n bulk@%h`, and exits non-zero if either child exits (`wait -n`, then kill the other); every registered task has a `time_limit` below `CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS` (3600) — assert over `app.tasks`.
> - [ ] **Step 2: Implement**; limits: dispatch 600 s, reapers/reconcilers 300 s, breaker 120 s, rollups 1,800 s per chunk (C7), discovery 900 s per chunk, analysis 900 s, webhooks 120 s. `CAPACITY.md` records the worker RAM budget (`2 pools × concurrency × ~150 MB`) and the DB connection demand formula from F20 (`api threads + worker procs + scheduler + scrapers ≤ PgBouncer pool`).
> - [ ] **Step 3:** commit `feat(workers): critical and bulk consumer pools in one service; task time limits; resumable discovery (F09)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B4.md

## Scope
Files/areas in scope:
- **B4** — Create `apps/workers/start.sh`, `docs/ops/CAPACITY.md`, `tests/unit/test_worker_start_script.py`, `tests/unit/test_celery_time_limits.py`, `tests/unit/test_discovery_chunk_cursor.py`, and the Alembic revision for the discovery cursor state if you persist it in a table. Modify `apps/workers/Dockerfile`, `apps/workers/app/workers/celery_app.py` (~`:130-240`), `libs/shared/app_shared/config.py`, `apps/workers/app/workers/tasks_strategy.py` (the discovery task).

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
- **B4** — CONTEXT.md unknown 4: `libs/shared/app_shared/strategy/discovery.py` DOES NOT EXIST. The real discovery task is `STRATEGY_DISCOVERY_RUN` implemented at `apps/workers/app/workers/tasks_strategy.py:1021` (enqueued from `app_shared/strategy/resolution.py:379` and `rediscovery.py`); the `strategy_discovery_runs` model is `libs/shared/app_shared/models/strategy.py:304`. Chunk THERE, following the plan's intent. `celery_app.py` is 300 lines.

Task-specific notes:
- **B4** — The plan asks for the cursor in a `strategy_discovery_state` row - that means a NEW TABLE and therefore an Alembic revision. This packet is consequently treated as migration-bearing and is never dispatched alongside another migration task. Chain from the current head and keep one head.
- **B4** — `docs/ops/CAPACITY.md` is created here and EXTENDED by B6 in a later wave - leave a clearly delimited section for the node-addressing decision rather than pre-writing it.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

