# Packet PB-7 — Phase B: Stage B — Durable work, reliable schedules
Model: sonnet   Parallel-safe: yes (with PB-2)   Depends on: PB-1
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B7 — Non-blocking API middleware with deadlines everywhere (F15)
Acceptance criteria:
- `apps/api/app/rate_limit.py` and `abuse_limit.py` hold no synchronous `redis.Redis`; they use `redis.asyncio.Redis` from `REDIS_URL` with `socket_connect_timeout=0.25` and `socket_timeout=0.25`, and one Lua script performing atomic `INCR` + `EXPIRE`-on-first-hit (fakeredis asserts exactly one script call).
- Unauthenticated admission is keyed by client IP with bounded key cardinality `API_RATE_LIMIT_MAX_KEYS = 100_000`; authenticated quotas are keyed by workspace.
- A Redis stub that sleeps 2 s makes the middleware fail open within 300 ms (via `asyncio.wait_for`) while a concurrent `/live` request completes.
- `libs/shared/app_shared/redis_client.py` (~`:26-50`) reads `socket_timeout`, `socket_connect_timeout` and `health_check_interval` from new settings `REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0` and `REDIS_CONNECT_TIMEOUT_SECONDS: float = 1.0`.
- `libs/shared/app_shared/database.py` sets `connect_args={'options': f'-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}'}` and `pool_timeout=DB_POOL_ACQUIRE_TIMEOUT_SECONDS`; API 15,000 ms, workers 120,000 ms, sweeps override per task.
- The `load` pytest marker is REGISTERED in `/srv/crawmatic/crawmatic/pyproject.toml`. `tests/load/test_api_with_slow_dependencies.py` (marker `load`) drives 200 concurrent `/v1/products` requests against a slow-Redis stub with `statement_timeout` forced, asserting p95 < 2 s and zero 5xx from event-loop stalls.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B7: Non-blocking API middleware with deadlines everywhere (F15) — *parallel-safe*
>
> **Files:**
> - Modify: `apps/api/app/rate_limit.py:130-190`, `apps/api/app/abuse_limit.py` (use `redis.asyncio.Redis` from `REDIS_URL` with `socket_connect_timeout=0.25`, `socket_timeout=0.25`; one Lua script for atomic `INCR` + `EXPIRE` on first hit; unauthenticated admission keyed by client IP with bounded key cardinality `API_RATE_LIMIT_MAX_KEYS = 100_000`; authenticated quotas keyed by workspace)
> - Modify: `libs/shared/app_shared/redis_client.py:26-50` (`socket_timeout`, `socket_connect_timeout`, `health_check_interval` from `REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0`, `REDIS_CONNECT_TIMEOUT_SECONDS: float = 1.0`)
> - Modify: `libs/shared/app_shared/database.py` (engine `connect_args={"options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"}`, `pool_timeout=DB_POOL_ACQUIRE_TIMEOUT_SECONDS`; API 15,000 ms, workers 120,000 ms, sweeps override per task)
> - Test: `tests/unit/test_rate_limit_async.py` (fakeredis async), `tests/unit/test_redis_client_timeouts.py`, `tests/unit/test_db_statement_timeout.py`, `tests/load/test_api_with_slow_dependencies.py`
>
> - [ ] **Step 1: Failing tests**: the middleware holds no sync `redis.Redis`; a Redis stub that sleeps 2 s makes the middleware fail open within 300 ms while a concurrent `/live` request completes (`asyncio.wait_for`); the counter increments and sets expiry in one script call (assert fakeredis script call count == 1).
> - [ ] **Step 2: Load test** — `tests/load/test_api_with_slow_dependencies.py` (marker `load`): API against a Python "slow Redis" stub (accepts connections, delays replies 3 s) and Postgres with `statement_timeout` forced; 200 concurrent `/v1/products` requests; assert p95 < 2 s and zero 5xx from event-loop stalls.
> - [ ] **Step 3:** commit `feat(api): async rate limiting with atomic counters; socket, connect, statement and pool deadlines (F15)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B7.md

## Scope
Files/areas in scope:
- **B7** — Modify `apps/api/app/rate_limit.py` (~`:130-190`), `apps/api/app/abuse_limit.py`, `libs/shared/app_shared/redis_client.py` (~`:26-50`), `libs/shared/app_shared/database.py`, `libs/shared/app_shared/config.py`, `pyproject.toml` (markers only). Create `tests/unit/test_rate_limit_async.py`, `tests/unit/test_redis_client_timeouts.py`, `tests/unit/test_db_statement_timeout.py`, `tests/load/test_api_with_slow_dependencies.py`.

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
- **B7** — `rate_limit.py` is 187 lines, `abuse_limit.py` 326, `redis_client.py` 55, `database.py` 378 - all confirmed present. Only `integration` is registered in `[tool.pytest.ini_options] markers` today; A1 added `browser` in Stage A - append `load`, do not replace the list.

Task-specific notes:
- **B7** — `libs/shared/app_shared/database.py` is shared by every service. A statement timeout that is too low breaks long sweeps - honour the per-role values in the criteria and keep an override hook for maintenance tasks.
- **B7** — The `load` test is a local stub test; it must not touch a real Redis or a real database.

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

