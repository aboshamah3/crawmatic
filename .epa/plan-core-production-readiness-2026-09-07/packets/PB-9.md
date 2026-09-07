# Packet PB-9 — Phase B: Stage B — Durable work, reliable schedules
Model: sonnet   Parallel-safe: no   Depends on: PB-1, PB-2, PB-3, PB-4, PB-5, PB-6, PB-7, PB-8
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w6   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B10 — Engine release 2 — OWNER GATE
Acceptance criteria:
- `docs/ops/RELEASE_2_2026-09.md` carries the plan's checklist with exact commands: quiesce (no RUNNING jobs); set `SCRAPE_RESULT_SPOOL_PATH` on the scrapers' volume; the new variable NAMES (`FLEET_HOST_*`, `SCRAPYD_MAX_PENDING_PER_NODE`, `CELERY_CRITICAL_CONCURRENCY`, `CELERY_BULK_CONCURRENCY`, `REDIS_*_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS`, `SCHEDULER_FAIR_QUEUE_ENABLED=true`); migration to head; deploy order migrate -> api -> worker -> scheduler -> scrapers -> scrapers-browser.
- Post-deploy checks are spelled out: `/health/scraping` green, two workers visible in `celery inspect ping`, a 50-target job shows intents `CONFIRMED` with `node_url`, spool `pending_count == 0` after the job.
- Step 1's rehearsal via `scripts/dr/rehearse_upgrade.sh` on the newest post-release-1 dump is ATTEMPTED; record every step duration in the doc. Determine the Postgres major from the dump header as A10 did and reuse that finding.
- Step 2 is an OWNER GATE: prepare, deploy nothing, change no Railway variable. Mark it deferred in the report.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B10: Engine release 2 — OWNER GATE
>
> **Files (EPA):** `docs/ops/RELEASE_2_2026-09.md`
>
> - [ ] **Step 1 (EPA):** rehearsal via `scripts/dr/rehearse_upgrade.sh` on the newest post-release-1 dump; release doc: quiesce (no RUNNING jobs); set `SCRAPE_RESULT_SPOOL_PATH` on the scrapers' volume; new variables (names): `FLEET_HOST_*`, `SCRAPYD_MAX_PENDING_PER_NODE`, `CELERY_CRITICAL_CONCURRENCY`, `CELERY_BULK_CONCURRENCY`, `REDIS_*_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS`, `SCHEDULER_FAIR_QUEUE_ENABLED=true`; migration to head; deploy order migrate → api → worker → scheduler → scrapers → scrapers-browser; post-deploy: `/health/scraping` green, two workers visible in `celery inspect ping`, a 50-target job shows intents `CONFIRMED` with `node_url`, spool `pending_count == 0` after the job.
> - [ ] **Step 2 (owner):** execute and record. Stage C starts after this gate.
>
> ---

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B10.md

## Scope
Files/areas in scope:
- **B10** — Create `docs/ops/RELEASE_2_2026-09.md`. Modify nothing else.

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
- **B10** — Read every Stage B report under the run dir's `reports/` for the settings, migrations, scripts and behaviour changes the release doc must list (especially B3's `SCHEDULER_FAIR_QUEUE_ENABLED` default flip and B1's spool path).

Task-specific notes:
- **B10** — Docker rehearsal is serialized; skip if a parallel sibling is running and note it in the report. ENOSPC -> BLOCKER + deferred, not a failure.
- **B10** — Variable NAMES only; never a value.

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

