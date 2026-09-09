# Packet PD-3 — Phase D: Stage D — Certify and expand
Model: sonnet   Parallel-safe: yes (with PD-2)   Depends on: PC-10
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: D-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### D5 — Daily cost and freshness scorecard
Acceptance criteria:
- `libs/shared/app_shared/maintenance/scorecard.py` adds task `MAINTENANCE_DAILY_SCORECARD` on the scheduler's durable cadence, writing one `fleet_daily_scorecard` row per UTC day with EVERY field the plan names: `provider_bytes`, `railway_cpu_seconds`, `railway_ram_gb_hours`, `railway_egress_gb`, `valid_fresh_matches`, `attempts_per_valid_fresh`, `browser_share`, `proxied_share`, `queue_oldest_seconds_p95`, `persistence_lag_seconds_p95`, `missing_metric_fraction`, `budget_reserved_usd`, `budget_settled_usd`, `backup_egress_gb`, `cost_per_valid_fresh_micro_usd`.
- Missing inputs are written as `NULL`, never 0 - `tests/unit/test_scorecard_fields.py` asserts both the full field set and the NULL behaviour.
- One Alembic revision creates `fleet_daily_scorecard`. Single head preserved.
- `GET /admin/scorecard?days=30` is added to `apps/api/app/routers/admin_ops.py` behind the same service-token guard as the rest of that router.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D5: Daily cost and freshness scorecard (deep dive §12 item 9) — *parallel-safe*
>
> **Files:**
> - Create: `libs/shared/app_shared/maintenance/scorecard.py` (task `MAINTENANCE_DAILY_SCORECARD` on the scheduler's durable cadence; one `fleet_daily_scorecard` row per UTC day: `provider_bytes`, `railway_cpu_seconds`, `railway_ram_gb_hours`, `railway_egress_gb`, `valid_fresh_matches`, `attempts_per_valid_fresh`, `browser_share`, `proxied_share`, `queue_oldest_seconds_p95`, `persistence_lag_seconds_p95`, `missing_metric_fraction`, `budget_reserved_usd`, `budget_settled_usd`, `backup_egress_gb`, `cost_per_valid_fresh_micro_usd`)
> - Create: `alembic/versions/<rev>_fleet_daily_scorecard.py`
> - Modify: `apps/api/app/routers/admin_ops.py` (`GET /admin/scorecard?days=30`)
> - Test: `tests/unit/test_scorecard_fields.py` (every field present; `NULL` for missing inputs, never 0)
>
> - [ ] **Step 1:** failing test; implement; commit `feat(ops): daily cost/freshness scorecard table and route (deep dive §12.9)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D5.md

## Scope
Files/areas in scope:
- **D5** — Create `libs/shared/app_shared/maintenance/scorecard.py`, `alembic/versions/<rev>_fleet_daily_scorecard.py`, `tests/unit/test_scorecard_fields.py`. Modify `apps/api/app/routers/admin_ops.py`, and the maintenance registry / task-name module as the repo convention requires.

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
- **D5** — `apps/api/app/routers/admin_ops.py` was CREATED by B9 in Stage B (CONTEXT.md unknown 5) and is guarded by `app.service_auth.require_service_token`, the same guard as `ops_metrics.py` - add the route there, do not create a second router. The inputs come from A5's and A7's gauges and A8's corrected billing unit; money is micro-USD.

Task-specific notes:
- **D5** — This is the only Alembic revision in Stage D - chain from the current head and keep one head.
- **D5** — Do not call the Railway API for real; the Railway-sourced fields come from the existing watchdog's stored output or are `NULL`.

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

