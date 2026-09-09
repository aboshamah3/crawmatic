# Packet PA-8 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: sonnet   Parallel-safe: no   Depends on: PA-1, PA-2, PA-3, PA-4, PA-5, PA-6, PA-7
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w5   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A10 — Engine release 1 — OWNER GATE (F20)
Acceptance criteria:
- `scripts/dr/rehearse_upgrade.sh` restores a dump into the compose Postgres, runs `alembic upgrade head`, then `scripts/preflight_regex_profiles.py` and `scripts/verify_grants.py`, timing each step and failing loudly on any non-zero exit.
- `docs/ops/RELEASE_1_2026-09.md` reproduces the plan's 11-point checklist IN ORDER with exact, copy-pasteable commands: disk relief + `main` SHA, SaaS `npx prisma migrate deploy` FIRST, quiesce, netledger buffer drain, the Railway variable NAMES table, provision roles + `verify_grants.py`, deploy order (migrate -> api -> worker -> scheduler -> scrapers -> scrapers-browser), post-deploy checks, image certification, the 20-target S-Tech smoke, and the rollback rule.
- The Postgres major used by the rehearsal is DETERMINED, not assumed: read the header of the newest dump under `/srv/crawmatic/backups/dr/` (`pg_restore -l <dump> | head`, or the dump header bytes) and use that major for the rehearsal container; LOG the finding in the release doc. Compose currently pins `postgres:17.5-bookworm` while the plan text says PostgreSQL 18 - record which is right.
- Step 1's rehearsal IS attempted (ASSUMPTIONS.md). It needs the DR gpg passphrase source already used by `scripts/dr/verify_restore.sh` (`DR_KEY_FILE`, default `/root/.crawmatic-dr/backup.key`) - reference it by NAME/location only. If disk or passphrase access blocks it, log a BLOCKER and mark the rehearsal deferred.
- Step 2's execution and step 3 are an OWNER GATE: prepare everything, deploy nothing, change no Railway variable, run no production command. Mark the gate deferred in the report.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A10: Engine release 1 — OWNER GATE (F20)
>
> Carries: all 2026-09-03 engine work (that plan's A1–A4, B1–B5, C1–C2, F1–F4, F9) **plus** Stage A of this plan. EPA prepares every artifact below and stops; the owner executes in one maintenance window.
>
> **Files (EPA):**
> - Create: `docs/ops/RELEASE_1_2026-09.md` (the checklist below with exact commands, in order)
> - Create: `scripts/dr/rehearse_upgrade.sh` (restore the latest dump into the compose Postgres 18, run `alembic upgrade head`, run `scripts/preflight_regex_profiles.py`, run `scripts/verify_grants.py`, time each step)
>
> - [ ] **Step 1 (EPA): rehearsal on a restored copy** — `sudo scripts/dr/rehearse_upgrade.sh /srv/crawmatic/backups/dr/<latest>` must finish green; record durations in the release doc (F20 "rehearse the upgrade on a restored production-sized database"; at 190 MB this is minutes, and D1 repeats it at projected size).
> - [ ] **Step 2 (EPA): release doc** in this order:
>   1. Disk relief done (0.1). `main` at the release SHA (0.2). Manifest written (`scripts/write_release_manifest.py`) and attestation built (`scripts/build_deployment_attestation.py`).
>   2. **SaaS migration first** (the only SaaS touch): from `/srv/crawmatic/saas/app`, `npx prisma migrate deploy` against production, confirming `20260904000000_cost_category_proxy_browser` applied. Without it the nightly SaaS margin job fails once the engine usage export carries the new transport counters.
>   3. **Quiesce admission**: refresh rule stays disabled; confirm `SELECT count(*) FROM scrape_jobs WHERE status IN ('RUNNING','PENDING')` = 0.
>   4. **Drain the netledger buffer** on the scraper services (09-03 B1 renamed payload keys): run the recorder flush and confirm `pending_count == 0` before the image swap.
>   5. Railway variables (names only; values by owner): all five engine services `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` unset or ≥ 8/4; API `FLEET_BUDGET_MONTHLY_CAP_USD_PROXY=75`, `FLEET_BUDGET_MONTHLY_CAP_USD_BROWSER=25`, `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker`; scrapers: `NETLEDGER_BUFFER_PATH` on the attached volume; scrapers-browser: `BROWSER_EGRESS_GUARD_ENABLED=true`, `BROWSER_SERVICE_WORKERS=block`; all: `EXTRACTION_REGEX_TIMEOUT_SECONDS=0.25`; Postgres: `pg_stat_statements` (A9); secrets split per `SECRETS_BY_COMPONENT.md` (scrapers lose admin/service tokens and use the `crawmatic_scraper` DSN).
>   6. Provision roles/grants: `scripts/provision_db_roles.sql` then `scripts/verify_grants.py` → exit 0.
>   7. Deploy the migration service (`alembic upgrade head`, straight to head; never stop at an intermediate revision because `c4b19e7a2f08` imports live model SQL), then api, worker, scheduler, scrapers, scrapers-browser from the tagged SHA.
>   8. Post-deploy: `/version` reports the SHA and `code_migration_head == db head`; `/ready` 200 with heartbeats present; seed caps `python scripts/seed_fleet_budget_cap.py --monthly-cap-usd 75 --scope-key proxy --months-ahead 1 --propose` then `--apply`, same for browser 25; `scripts/preflight_regex_profiles.py` against prod (read-only) → exit 0.
>   9. **Certify the image**: from the deployed scrapers-browser container run `pytest tests/integration/test_browser_egress_guard_image.py -m browser` (the build already ran it; this proves the runtime flags).
>   10. Smoke: one 20-target S-Tech direct job via the API; confirm targets pass through `STARTED` and `persisted_at` is set; confirm the ledger links ≥ 95% of attempts.
>   11. Rollback rule: image rollback is safe **only before** step 7's data migration; after it, roll forward (`docs/DEPLOY-ROLLBACK.md`).
> - [ ] **Step 3 (owner):** execute; paste the manifest path and the smoke job id into `.epa/<slug>/ASSUMPTIONS.md`. Stage B starts only after this gate is recorded as done.
>
> ---

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A10.md

## Scope
Files/areas in scope:
- **A10** — Create `docs/ops/RELEASE_1_2026-09.md`, `scripts/dr/rehearse_upgrade.sh`. Modify nothing else.

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
- **A10** — `scripts/dr/` holds `backup_prod.sh`, `dr_lib.sh`, `verify_restore.sh`, `install_schedule.sh`, `RUNBOOK.md`; `rehearse_upgrade.sh` does not exist yet. `dr_lib.sh:121-127` checks that `DR_KEY_FILE` is mode 600 and root-owned. Read every Stage A report under the run dir's `reports/` for the settings, migrations and scripts the release doc must list.

Task-specific notes:
- **A10** — Docker rehearsal is serialized; skip if a parallel sibling is running and note it in the report. ENOSPC -> BLOCKER + deferred, not a failure.
- **A10** — Never write a credential VALUE into the release doc - variable names and file locations only.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. Integration/docker checks: `docker compose up -d postgres redis` then `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration -q -m integration` (scoped to this packet's test files). **Serialized; skip if a parallel sibling is running, and note the skip in the report.** ENOSPC or an unavailable stack → BLOCKER + deferred, never a task failure.

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

