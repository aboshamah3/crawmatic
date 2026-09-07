# Packet PC-6 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: no   Depends on: PC-4
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w4   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C7 — Set-based daily rollups with keyset batches and checkpoints (F12)
Acceptance criteria:
- Rollup semantics become LATEST ELIGIBLE OBSERVATION per `(workspace, variant, match, day)`, then aggregate - a competitor observed ten times counts once.
- `libs/shared/app_shared/maintenance/rollup_sql.py` renders one `INSERT ... SELECT` with `DISTINCT ON (workspace_id, product_variant_id, match_id) ... ORDER BY scraped_at DESC` in a CTE, joined to `variant_price_states`, grouped per variant, `ON CONFLICT DO UPDATE`, with a keyset window `(workspace_id, product_variant_id) > (:w, :v) LIMIT 5000`. The loop in `rollups.py` (~`:265-380`) is replaced by it.
- The measured range predicate is kept exactly: `scraped_at >= day_start AND scraped_at < day_end`.
- One Alembic revision creates `rollup_completion(date PK, last_key_workspace_id, last_key_variant_id, complete BOOL, updated_at)` - C8 reads `complete`. Single head preserved.
- Each batch commits; the Celery `time_limit` is 1,800 s per invocation with re-enqueue until `complete`. `tests/integration/test_rollup_set_based.py` proves the 10-vs-1 skew case, that averages match a hand computation, and that a crash mid-batch resumes from the checkpoint without double counting.
- The `benchmark` pytest marker is REGISTERED in `/srv/crawmatic/crawmatic/pyproject.toml`. `tests/benchmarks/test_rollup_500k.py` (marker `benchmark`) exists but is RUN in D1, not here.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C7: Set-based daily rollups with keyset batches and checkpoints (F12)
>
> **Files:**
> - Modify: `libs/shared/app_shared/maintenance/rollups.py:114-150` (semantics: **latest eligible observation per `(workspace, variant, match, day)`**, then aggregate), `:265-380` (replace the loop)
> - Create: `libs/shared/app_shared/maintenance/rollup_sql.py` (one `INSERT ... SELECT` with `DISTINCT ON (workspace_id, product_variant_id, match_id) ... ORDER BY scraped_at DESC` in a CTE, joined to `variant_price_states`, grouped per variant, `ON CONFLICT DO UPDATE`; keyset window `(workspace_id, product_variant_id) > (:w, :v) LIMIT 5000`)
> - Create: `alembic/versions/<rev>_rollup_completion.py` (`rollup_completion(date PK, last_key_workspace_id, last_key_variant_id, complete BOOL, updated_at)`; C8 reads `complete`)
> - Test: `tests/unit/test_rollup_sql_render.py`, `tests/integration/test_rollup_set_based.py` (10-vs-1 observation skew: a competitor observed ten times counts once; averages match a hand computation; a crash mid-batch resumes from the checkpoint without double counting), `tests/benchmarks/test_rollup_500k.py` (marker `benchmark`, run in D1)
>
> - [ ] **Step 1:** failing tests. **Step 2:** implement; keep the range predicate the deep dive measured (`scraped_at >= day_start AND scraped_at < day_end`); commit per batch; `time_limit` 1,800 s per Celery invocation with re-enqueue until `complete`.
> - [ ] **Step 3:** commit `perf(rollups): set-based daily rollup on latest-per-match semantics with keyset batches and checkpoints (F12)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C7.md

## Scope
Files/areas in scope:
- **C7** — Modify `libs/shared/app_shared/maintenance/rollups.py` (~`:114-150`, `:265-380`), `pyproject.toml` (markers only). Create `libs/shared/app_shared/maintenance/rollup_sql.py`, `alembic/versions/<rev>_rollup_completion.py`, `tests/unit/test_rollup_sql_render.py`, `tests/integration/test_rollup_set_based.py`, `tests/benchmarks/test_rollup_500k.py`.

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
- **C7** — `maintenance/rollups.py` is 607 lines; both cited ranges fit. Markers registered so far: `integration` (pre-existing), `browser` (A1), `load` (B7) - append `benchmark`, do not replace the list. B4's Celery annotations already set rollups to 1,800 s per chunk; align with them.

Task-specific notes:
- **C7** — The plan's own text defers the 500k benchmark run to D1 - write the test, do not run it here.
- **C7** — Integration run needs the compose Postgres: serialized; skip if a parallel sibling is running and note it in the report.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`
3. Integration/docker checks: `docker compose up -d postgres redis` then `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration -q -m integration` (scoped to this packet's test files). **Serialized; skip if a parallel sibling is running, and note the skip in the report.** ENOSPC or an unavailable stack → BLOCKER + deferred, never a task failure.

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

