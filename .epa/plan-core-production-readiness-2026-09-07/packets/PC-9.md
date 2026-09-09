# Packet PC-9 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: yes (with PC-1)   Depends on: PB-9
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C10 — Backups from inside Railway, measured, encrypted, off-host (F21)
Acceptance criteria:
- `apps/dr-backup/` contains `Dockerfile` (postgres-client matching the production major, gpg, python), `backup.sh`, `serve.py` (a token-protected private-network file server, ~40 lines of `http.server`) and `railway.json` (cron `0 */4 * * *`, volume at `/backups`).
- The production Postgres major is DETERMINED, not assumed: read the header of the newest dump under `/srv/crawmatic/backups/dr/` (`pg_restore -l <dump> | head`, or the dump header bytes) and pin postgres-client to THAT major in the Dockerfile; LOG the finding in `scripts/dr/RUNBOOK.md`. Compose pins `postgres:17.5-bookworm` while the plan text says PostgreSQL 18 - record which is right.
- `scripts/dr/backup_prod.sh` becomes the PULL side: this host fetches only the newest encrypted set daily from `dr-backup` over the private network and keeps 2 days locally. Volume retention: 4-hourly sets for 2 days, daily for 14, weekly for 8.
- `scripts/dr/verify_restore.sh` restores into the compose Postgres and ADDITIONALLY restores the netledger buffer, the result spool (B1), the evidence directory listing (C5) and a config snapshot from the manifest; it asserts migration-head equality and times every step.
- `scripts/dr/dr_lib.sh` records `bytes_exported` per backup from `pg_dump | wc -c` before encryption and `bytes_on_wire` from the private connection, and posts both to `/admin/ops/backup-report`.
- `scripts/dr/RUNBOOK.md` states RPO = 4 h and the residual risk verbatim: the second location is the same Railway account and region; losing the account loses both copies; the host pull copy is the third location; the gpg passphrase must be held in the owner's password manager, not only on this host.
- `tests/unit/test_dr_backup_script_lint.py` asserts no public hostname appears and `PGHOST` comes from the private `*.railway.internal` name. `tests/unit/test_backup_report_fields.py` covers the report payload.
- Step 3 is an OWNER GATE inside C11 (create the service and volume, set the gpg passphrase and pull-token variable NAMES, run one manual backup, run `verify_restore.sh`, confirm public egress drops toward 0). Prepare the exact commands and mark it deferred.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C10: Backups from inside Railway, measured, encrypted, off-host; restore drill covers config, buffers and evidence (F21, deep dive §9.2)
>
> **Files:**
> - Create: `apps/dr-backup/Dockerfile` (postgres-client **18**, gpg, python), `apps/dr-backup/backup.sh`, `apps/dr-backup/serve.py` (token-protected private-network file server, ~40 lines of `http.server`), `apps/dr-backup/railway.json` (cron `0 */4 * * *`, volume at `/backups`)
> - Modify: `scripts/dr/backup_prod.sh` → becomes the **pull** side: this host fetches only the newest encrypted set daily from `dr-backup` over the private network and keeps 2 days locally
> - Modify: `scripts/dr/verify_restore.sh` (restore into the compose Postgres 18; additionally restore the netledger buffer, the result spool, the evidence directory listing and a config snapshot from the manifest; assert migration-head equality; time every step)
> - Modify: `scripts/dr/dr_lib.sh` (record `bytes_exported` per backup from `pg_dump | wc -c` before encryption and `bytes_on_wire` from the private connection; post both to `/admin/ops/backup-report`)
> - Modify: `scripts/dr/RUNBOOK.md` (RPO 4 h stated; residual risk: "second location is the same Railway account and region; losing the account loses both copies — the host pull copy is the third location; the gpg passphrase must be held in the owner's password manager, not only on this host")
> - Test: `tests/unit/test_dr_backup_script_lint.py` (no public hostname; `PGHOST` from the private `*.railway.internal` name), `tests/unit/test_backup_report_fields.py`
>
> - [ ] **Step 1:** failing tests. **Step 2:** implement; retention on the volume: 4-hourly sets for 2 days, daily for 14, weekly for 8. **Step 3 (owner, in C11):** create the `dr-backup` service with the volume, set the gpg passphrase and the pull token variables; run one manual backup; run `verify_restore.sh` against it; confirm Postgres public egress drops to ≈ 0 the next day in Railway metrics (the deep dive's 0.853 GB/day idle egress is the baseline to beat).
> - [ ] **Step 4:** commit `feat(dr): backups run inside Railway on the private network, measured and encrypted; host keeps a pull copy; restore drill covers buffers and evidence (F21)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C10.md

## Scope
Files/areas in scope:
- **C10** — Create `apps/dr-backup/Dockerfile`, `apps/dr-backup/backup.sh`, `apps/dr-backup/serve.py`, `apps/dr-backup/railway.json`, `tests/unit/test_dr_backup_script_lint.py`, `tests/unit/test_backup_report_fields.py`. Modify `scripts/dr/backup_prod.sh`, `scripts/dr/verify_restore.sh`, `scripts/dr/dr_lib.sh`, `scripts/dr/RUNBOOK.md`.

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
- **C10** — `dr_lib.sh:121-127` confirms the gpg passphrase source: env `DR_KEY_FILE`, default `/root/.crawmatic-dr/backup.key`, required to be mode 600 and root-owned. Reference it by NAME/location only - never read or print the value. `/etc/cron.d/crawmatic-dr` currently runs `backup_prod.sh` every 4 h and `verify_restore.sh` daily at 03:20 over the PUBLIC DB endpoint; the deep dive's 0.853 GB/day idle egress is the baseline to beat.

Task-specific notes:
- **C10** — Destructive firewall: never delete a backup, never create a Railway service, never change a Railway variable, never run a production `pg_dump`. Prepare and stop.
- **C10** — A10's `scripts/dr/rehearse_upgrade.sh` already exists - reuse it rather than duplicating restore logic.

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

