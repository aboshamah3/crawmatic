# Packet PB-8 — Phase B: Stage B — Durable work, reliable schedules
Model: sonnet   Parallel-safe: yes (with PB-1)   Depends on: PA-8
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B8 — Readiness probes that cannot pile up; liveness/dependency/scraping split (F16)
Acceptance criteria:
- `apps/api/app/routers/ready.py` (~`:200-262`) uses ONE module-level `ThreadPoolExecutor(max_workers=2)` plus a probe-generation lock: while the previous generation runs, requests return its last result with `"stale": true`. Each probe opens its OWN session via `get_session()`.
- `tests/unit/test_ready_no_overlap.py`: 20 concurrent `/ready` calls while the DB probe blocks -> at most 2 probe threads exist and 19 responses carry `"stale": true`; the migration probe and the DB probe never share a session object (distinct ids).
- `apps/api/app/routers/health.py` provides `GET /live` (process up), `GET /ready` (DB/Redis/migration/heartbeats only) and `GET /health/scraping` (breaker evaluation age, freshness fraction, oldest pending target). `/health/scraping` NEVER gates `/ready`. Mounted in `apps/api/app/main.py`.
- `READY_REQUIRED_HEARTBEAT_SERVICES` stays on `/ready` (a missing scheduler heartbeat is a dependency failure); breaker and freshness move to `/health/scraping`. `tests/unit/test_health_scraping_signal.py`: `/ready` returns 200 while `/health/scraping` reports `freshness_fraction_24h: 0.0` and `status: "degraded"`.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B8: Readiness probes that cannot pile up; liveness, dependency and scraping health split (F16) — *parallel-safe*
>
> **Files:**
> - Modify: `apps/api/app/routers/ready.py:200-262` (one module-level `ThreadPoolExecutor(max_workers=2)`; a probe-generation lock: if the previous generation is still running, return its last result with `"stale": true`; each probe opens **its own** session via `get_session()`; B7's driver-level timeouts make `wait=False` unnecessary)
> - Create: `apps/api/app/routers/health.py` (`GET /live` → process up; `GET /ready` → DB/Redis/migration/heartbeats only; `GET /health/scraping` → breaker evaluation age, freshness fraction, oldest pending target; never gates `/ready`)
> - Modify: `apps/api/app/main.py` (mount)
> - Test: `tests/unit/test_ready_no_overlap.py`, `tests/unit/test_health_scraping_signal.py`
>
> - [ ] **Step 1: Failing tests**: 20 concurrent `/ready` calls while the DB probe blocks → at most 2 probe threads exist and 19 responses carry `"stale": true`; a stuck migration probe never shares a session object with the DB probe (distinct ids); `/ready` returns 200 while `/health/scraping` reports `freshness_fraction_24h: 0.0` and `status: "degraded"`.
> - [ ] **Step 2: Implement**; keep `READY_REQUIRED_HEARTBEAT_SERVICES` on `/ready` (09-03 F1) because a missing scheduler heartbeat is a dependency failure; breaker and freshness move to `/health/scraping`.
> - [ ] **Step 3:** commit `feat(api): bounded readiness probes with own sessions; /live, /ready, /health/scraping split (F16)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B8.md

### B9 — Heartbeats for every process class and alerts to an owner (F22)
Acceptance criteria:
- Scrapyd nodes emit `heartbeat:scraper:<node>` every 30 s from `scrapyd_app.py`; workers emit per pool (`critical@`, `bulk@`) matching B4's node names.
- `libs/shared/app_shared/opsmetrics/rules.py` gains every rule the plan lists, each with a fire/no-fire unit test at threshold +/- 1: `freshness_fraction_24h < 0.95`; `queue_oldest_pending_seconds > 3600`; `persistence_pending_batches > 16` or `persistence_quarantined_batches > 0`; `breaker_seconds_since_evaluation > 900`; `costauth_denials_1h{reason=BUDGET} > 0`; `disk_free_fraction < 0.15` (host and volumes); `restore_verify_failed`; `ledger_linked_attempt_fraction_24h < 0.95`; `dispatch_ambiguous_intents > 0 for 10 min`; `heartbeat_missing{service}`.
- `GET /admin/alerts/active` exists for the existing ops cron to poll; delivery stays the existing `crawmatic-ops-alerts` channel - NO new provider or external service.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B9: Heartbeats for every process class and alerts to an owner (F22, audit §13 Operations) — *parallel-safe*
>
> **Files:**
> - Modify: `libs/shared/app_shared/heartbeat.py` (scrapyd nodes emit `heartbeat:scraper:<node>` every 30 s from `scrapyd_app.py`; workers per pool `critical@`/`bulk@`)
> - Modify: `libs/shared/app_shared/opsmetrics/rules.py` (rules: `freshness_fraction_24h < 0.95`; `queue_oldest_pending_seconds > 3600`; `persistence_pending_batches > 16` or `persistence_quarantined_batches > 0`; `breaker_seconds_since_evaluation > 900`; `costauth_denials_1h{reason=BUDGET} > 0`; `disk_free_fraction < 0.15` (host and volumes); `restore_verify_failed`; `ledger_linked_attempt_fraction_24h < 0.95`; `dispatch_ambiguous_intents > 0 for 10 min`; `heartbeat_missing{service}`)
> - Modify: `apps/api/app/routers/admin_ops.py` (`GET /admin/alerts/active` for the existing ops cron to poll; delivery stays the existing `crawmatic-ops-alerts` channel, no new provider)
> - Test: `tests/unit/test_ops_rules_new.py`
>
> - [ ] **Step 1:** failing tests per rule with synthetic snapshots (fire/no-fire at threshold ± 1). Implement. Commit `feat(ops): scraper/worker-pool heartbeats; freshness, backlog, budget, disk and restore alert rules (F22)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B9.md

## Scope
Files/areas in scope:
- **B8** — Modify `apps/api/app/routers/ready.py` (~`:200-262`), `apps/api/app/main.py`. Create `apps/api/app/routers/health.py`, `tests/unit/test_ready_no_overlap.py`, `tests/unit/test_health_scraping_signal.py`.
- **B9** — Create `apps/api/app/routers/admin_ops.py` and mount it in `apps/api/app/main.py`; create `tests/unit/test_ops_rules_new.py`. Modify `libs/shared/app_shared/heartbeat.py`, `libs/shared/app_shared/opsmetrics/rules.py`, and the scrapyd app that emits heartbeats.

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
- **B8** — `ready.py` is 494 lines; `_run_with_timeout`/`ReadyResponse` were confirmed in the cited region. B7's driver-level timeouts (earlier wave) make `wait=False` unnecessary - rely on them.
- **B9** — CONTEXT.md unknown 5: `apps/api/app/routers/admin_ops.py` DOES NOT EXIST - CREATE it and mount it in `main.py`. The existing admin routers are `admin.py` and `jobs_admin.py`; there is also a tenant-facing `alerts.py` (workspace-scoped `/v1/alerts/*`) and an operator `ops_metrics.py` guarded by `app.service_auth.require_service_token`. The new cross-workspace `/admin/alerts/active` must use the SAME service-token guard as `ops_metrics.py`, not the tenant auth seam. D5 later adds `GET /admin/scorecard` to this same new router. `heartbeat.py` is 460 lines, `opsmetrics/rules.py` 1668.

Task-specific notes:
- **B8** — You share `apps/api/app/main.py` with B9 in this same packet - do both router mounts in one edit.
- **B9** — The metric names consumed by these rules are the contract A5 and A7 already shipped - use them verbatim, do not invent new ones.
- **B9** — You share `apps/api/app/main.py` with B8 in this same packet - do both router mounts in one edit.

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

