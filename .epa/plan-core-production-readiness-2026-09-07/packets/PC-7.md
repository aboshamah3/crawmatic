# Packet PC-7 — Phase C: Stage C — Efficient and correct results/cost
Model: sonnet   Parallel-safe: no   Depends on: PC-6
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w5   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C8 — Retention gated on per-key coverage and the completion watermark (F13)
Acceptance criteria:
- `libs/shared/app_shared/maintenance/retention.py` (~`:77-140`): `_rollups_cover_stmt` becomes per-key - `SELECT DISTINCT workspace_id, product_variant_id, (scraped_at AT TIME ZONE 'UTC')::date FROM <partition> EXCEPT SELECT workspace_id, product_variant_id, date FROM variant_price_daily_rollups WHERE date >= :d0 AND date < :d_n` - AND every date in the range must have `rollup_completion.complete = true` (C7's table).
- `tests/integration/test_retention_partial_rollup_multi_tenant.py`: two workspaces, three variants; rolling up only one variant on one date retains the partition with reason `partitions_skipped_pending_rollups`; completing both makes it eligible.
- `docs/RETENTION_POLICY.md` §2.3 documents the two gates and states that failed and no-price observations are represented by the rollup's `comparable_competitor_count` semantics and are never dropped silently.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C8: Retention gated on per-key coverage and the completion watermark (F13) — depends on C7
>
> **Files:**
> - Modify: `libs/shared/app_shared/maintenance/retention.py:77-140` (`_rollups_cover_stmt` becomes per-key: `SELECT DISTINCT workspace_id, product_variant_id, (scraped_at AT TIME ZONE 'UTC')::date FROM <partition> EXCEPT SELECT workspace_id, product_variant_id, date FROM variant_price_daily_rollups WHERE date >= :d0 AND date < :d_n`; **and** every date in the range must have `rollup_completion.complete = true`)
> - Modify: `docs/RETENTION_POLICY.md` §2.3 (document the two gates; state that failed and no-price observations are represented by the rollup's `comparable_competitor_count` semantics, never dropped silently)
> - Test: `tests/integration/test_retention_partial_rollup_multi_tenant.py` (two workspaces, three variants; roll up only one variant on one date → partition retained with reason `partitions_skipped_pending_rollups`; complete both → eligible)
>
> - [ ] **Step 1:** failing integration test. **Step 2:** implement. **Step 3:** commit `fix(retention): drop only on per-key rollup coverage plus completion watermark (F13)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C8.md

## Scope
Files/areas in scope:
- **C8** — Modify `libs/shared/app_shared/maintenance/retention.py` (~`:77-140`), `docs/RETENTION_POLICY.md`. Create `tests/integration/test_retention_partial_rollup_multi_tenant.py`.

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
- **C8** — `maintenance/retention.py` is 212 lines; the cited range fits. Depends on C7's `rollup_completion` table having landed. Partitioned tables never get a bulk `DELETE`; drops are whole partitions (`docs/RETENTION_POLICY.md` SC-003).

Task-specific notes:
- **C8** — No Alembic revision here. `docs/RETENTION_POLICY.md` is edited again by C9 in the next wave - keep your §2.3 edit self-contained.
- **C8** — Integration run needs the compose Postgres: serialized; skip if a parallel sibling is running and note it in the report.

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

