# Packet PC-8 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: no   Depends on: PC-7
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w6   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C9 — Retention per data class; ledger child summarization; partitioning (F14)
Acceptance criteria:
- `libs/shared/app_shared/config.py` gains `RETENTION_REQUEST_ATTEMPTS_DAYS=90`, `RETENTION_PRICE_OBSERVATIONS_DAYS=180`, `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS=730`, `RETENTION_NETWORK_OPERATION_CHILDREN_DAYS=30`, `RETENTION_NETWORK_OPERATIONS_DAYS=730`, `RETENTION_COST_ALLOCATIONS_DAYS=730`, `RETENTION_SCRAPE_JOB_TARGETS_DAYS=90`, `RETENTION_DISPATCH_INTENTS_DAYS=30`, `RETENTION_COSTAUTH_RESERVATIONS_DAYS=30`, and `RETENTION_ENABLED_CLASSES: list[str] = []` - NOTHING drops until the owner lists the class.
- One Alembic revision creates `network_operations_p` partitioned by `RANGE (created_at)` monthly, copies rows in keyset batches, swaps names in one transaction, and lets the existing partition guard re-apply RLS policies; `scripts/rls_verify.py` must pass afterwards. Single head preserved.
- `libs/shared/app_shared/maintenance/ledger_summaries.py` adds task `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`: for parent operations older than 30 d AND settled (a `cost_settlements` row exists), write one `network_operation_resource_summaries(parent_operation_id, child_count, bytes_by_host_class JSONB, duration_ms_sum)` row and delete the children; the parent keeps exact totals. An UNSETTLED parent's children are kept (tested), and totals are equal before and after.
- `libs/shared/app_shared/maintenance/registry.py` registers the new families with their settings and `feeds_rollups=False`.
- `opsmetrics/emit.py` gains `crawmatic_bytes_per_physical_attempt`, `crawmatic_bytes_per_logical_target`, `crawmatic_dead_tuple_fraction{table}`, `crawmatic_index_bytes{table}`, `crawmatic_partition_maintenance_seconds`.
- `docs/RETENTION_POLICY.md`: every 'PENDING OWNER' is replaced by the decided default plus a literal 'Ratified by owner on ____' line for the owner to fill before enabling.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C9: Retention per data class; ledger child summarization; partitioning of high-volume families (F14, deep dive §9.1) — depends on C8
>
> **Files:**
> - Modify: `libs/shared/app_shared/config.py` (`RETENTION_REQUEST_ATTEMPTS_DAYS=90`, `RETENTION_PRICE_OBSERVATIONS_DAYS=180`, `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS=730`, new `RETENTION_NETWORK_OPERATION_CHILDREN_DAYS=30`, `RETENTION_NETWORK_OPERATIONS_DAYS=730`, `RETENTION_COST_ALLOCATIONS_DAYS=730`, `RETENTION_SCRAPE_JOB_TARGETS_DAYS=90`, `RETENTION_DISPATCH_INTENTS_DAYS=30`, `RETENTION_COSTAUTH_RESERVATIONS_DAYS=30`, `RETENTION_ENABLED_CLASSES: list[str] = []` — nothing drops until the owner lists the class)
> - Create: `alembic/versions/<rev>_partition_network_operations.py` (create `network_operations_p` partitioned by `RANGE (created_at)` monthly; copy in keyset batches; swap names in one transaction; RLS policies re-applied by the existing partition guard; `scripts/rls_verify.py` must pass)
> - Create: `libs/shared/app_shared/maintenance/ledger_summaries.py` (task `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`: for parent operations older than 30 d **and** settled (a `cost_settlements` row exists), write one `network_operation_resource_summaries(parent_operation_id, child_count, bytes_by_host_class JSONB, duration_ms_sum)` row and delete the children; the parent keeps exact totals)
> - Modify: `libs/shared/app_shared/maintenance/registry.py` (register the new families with their settings, `feeds_rollups=False`)
> - Modify: `libs/shared/app_shared/opsmetrics/emit.py` (`crawmatic_bytes_per_physical_attempt`, `crawmatic_bytes_per_logical_target`, `crawmatic_dead_tuple_fraction{table}`, `crawmatic_index_bytes{table}`, `crawmatic_partition_maintenance_seconds`)
> - Modify: `docs/RETENTION_POLICY.md` (every "PENDING OWNER" replaced by the decided default plus the line "Ratified by owner on ____" for the owner to fill before enabling)
> - Test: `tests/unit/test_retention_registry_classes.py`, `tests/integration/test_ledger_child_summarization.py` (an unsettled parent's children are kept; a settled one's are summarized; totals equal), `tests/integration/test_network_operations_partition_swap.py`
>
> - [ ] **Step 1:** failing tests. **Step 2:** implement. **Step 3:** commit `feat(retention): per-class retention with owner ratification switch; ledger child summaries; network_operations partitioned (F14)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C9.md

## Scope
Files/areas in scope:
- **C9** — Modify `libs/shared/app_shared/config.py`, `libs/shared/app_shared/maintenance/registry.py`, `libs/shared/app_shared/opsmetrics/emit.py`, `docs/RETENTION_POLICY.md`. Create `alembic/versions/<rev>_partition_network_operations.py`, `libs/shared/app_shared/maintenance/ledger_summaries.py`, `tests/unit/test_retention_registry_classes.py`, `tests/integration/test_ledger_child_summarization.py`, `tests/integration/test_network_operations_partition_swap.py`.

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
- **C9** — Depends on C8. `opsmetrics/emit.py` was previously edited by A5 and A7 (Stage A) - append gauges, do not rewrite the module. Owner decision 11 fixes the retention numbers; owner ratifies before retention is enabled in production, which is why `RETENTION_ENABLED_CLASSES` ships empty.

Task-specific notes:
- **C9** — The partition swap is the highest-risk migration in the plan. It runs ONLY against the compose database here - never against production. Record the swap duration; A10/B10/C11's rehearsal repeats it at projected size in D1.
- **C9** — Integration runs need the compose Postgres: serialized; skip if a parallel sibling is running and note it in the report.

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

