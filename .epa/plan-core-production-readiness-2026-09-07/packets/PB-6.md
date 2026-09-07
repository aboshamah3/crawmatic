# Packet PB-6 — Phase B: Stage B — Durable work, reliable schedules
Model: opus   Parallel-safe: yes (with PB-5)   Depends on: PB-2, PB-4
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w5   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B6 — Capacity-aware placement persisted on the intent; bounded node queues (F11)
Acceptance criteria:
- `libs/shared/app_shared/jobs/nodes.py` gains `choose_node(domain, nodes, *, loads: dict[str, NodeLoad], max_pending: int) -> str | None`; `select_node` is kept for the single-node case only.
- `libs/shared/app_shared/jobs/node_load.py` provides `read_node_loads(client, nodes, redis, ttl=10) -> dict[str, NodeLoad]` built on `ScrapydDispatchClient.daemon_status` (`scrapyd/client.py:585`) and cached in Redis.
- Plan tests pass: four nodes spread `amazon.sa` batches by least `running + pending`; a node at `max_pending` is skipped; all-saturated returns `None`; retries of an existing intent never call `choose_node` (they read `intent.node_url`); an unreachable node (`daemon_status` -> `None`) is treated as saturated for 60 s.
- `tasks_jobs.py` (~`:707`, `:1253`) uses `choose_node`; on `None` it DEFERS the batch and does not POST. New setting `SCRAPYD_MAX_PENDING_PER_NODE: int = 4`; the browser `scrapyd.conf` keeps `max_proc = 1` per node.
- `docs/ops/CAPACITY.md` gains the node-addressing decision: nodes are DISTINCT Railway services `scrapers-browser-1..n`, each with its own private hostname in `SCRAPYD_BROWSER_URLS`; Railway replicas share one hostname and cannot be reconciled by job id, so replicas are not used for scrapyd nodes.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B6: Capacity-aware placement persisted on the intent; bounded node queues; addressable nodes (F11)
>
> **Files:**
> - Modify: `libs/shared/app_shared/jobs/nodes.py` (`choose_node(domain, nodes, *, loads: dict[str, NodeLoad], max_pending: int) -> str | None`; `select_node` kept for the single-node case only)
> - Create: `libs/shared/app_shared/jobs/node_load.py` (`read_node_loads(client, nodes, redis, ttl=10) -> dict[str, NodeLoad]` using `ScrapydDispatchClient.daemon_status` (`client.py:585`), cached in Redis)
> - Modify: `apps/workers/app/workers/tasks_jobs.py:707`, `:1253` (use `choose_node`; on `None` defer the batch, do not POST)
> - Modify: `docs/ops/CAPACITY.md` (nodes are **distinct Railway services** `scrapers-browser-1..n`, each with its own private hostname in `SCRAPYD_BROWSER_URLS`; Railway replicas share one hostname and cannot be reconciled by job id, so they are not used for scrapyd nodes)
> - Test: `tests/unit/test_choose_node.py`, `tests/unit/test_node_load_cache.py`
>
> - [ ] **Step 1: Failing tests**: four nodes, batches for `amazon.sa` spread across nodes by least `running + pending`; a node at `max_pending` is skipped; when all nodes are saturated `choose_node` returns `None`; retries of an existing intent never call `choose_node` (they read `intent.node_url`); an unreachable node (`daemon_status` → `None`) is treated as saturated for 60 s.
> - [ ] **Step 2: Implement**; `SCRAPYD_MAX_PENDING_PER_NODE: int = 4`; browser `scrapyd.conf` keeps `max_proc = 1` per node.
> - [ ] **Step 3:** commit `feat(dispatch): load-aware node placement persisted on the intent; bounded scrapyd queues (F11)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B6.md

## Scope
Files/areas in scope:
- **B6** — Modify `libs/shared/app_shared/jobs/nodes.py`, `apps/workers/app/workers/tasks_jobs.py` (~`:707`, `:1253`), `docs/ops/CAPACITY.md`. Create `libs/shared/app_shared/jobs/node_load.py`, `tests/unit/test_choose_node.py`, `tests/unit/test_node_load_cache.py`.

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
- **B6** — `jobs/nodes.py` is only 38 lines (a thin file - the `choose_node` rewrite is genuinely new code). `scrapyd/client.py`'s `daemon_status` is confirmed near line 585. B2 (earlier wave) persisted `node_url` on the intent - read it, do not re-derive placement on retries. `docs/ops/CAPACITY.md` was created by B4 in an earlier wave; append, do not rewrite it.

Task-specific notes:
- **B6** — No Alembic revision in this task. Depends on B2 (intent `node_url`) and B4 (CAPACITY.md) having landed.
- **B6** — You are running in parallel with B5. Stay out of `libs/scrape-core/scrape_core/limiter.py`, `targets.py`, `enums.py`, `config.py` and `models/domain_rules.py`.

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

