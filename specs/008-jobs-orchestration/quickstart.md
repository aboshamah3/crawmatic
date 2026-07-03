# Quickstart & Validation: Jobs & Orchestration

How to validate this feature. Two tiers: **unit** (runs in this build env — no Postgres/Redis/Scrapyd) and **live** (authored + skip-marked, runs on a full-stack host). No test contacts a real competitor domain.

## Prerequisites

- uv workspace synced: `uv sync --all-packages` (never plain `uv sync` — it wipes workspace-member deps).
- Unit tier needs nothing else. Live tier needs a reachable Postgres (via PgBouncer), Redis, and a Scrapyd HTTP node with basic auth (env: `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `REDIS_URL`, `SCRAPYD_HTTP_URLS`, `SCRAPYD_USERNAME`, `SCRAPYD_PASSWORD`).

## Unit validation (this env)

```bash
uv run pytest tests/unit -q
```

Covers (maps to Success Criteria / Functional Requirements):
- **Models / migration render** (`test_jobs_models.py`, `test_jobs_rls.py`, `test_migration_offline_jobs.py`) — column shapes; `unique(scrape_job_id, match_id)`; `unique(workspace_id, id)`; composite target→job FK; RLS DDL for both; `alembic upgrade head --sql` renders both tables + RLS; single head; `down_revision == 2db33dea5e14` (FR-001..005).
- **Batching** (`test_jobs_batching.py`) — group by domain+mode; HTTP batch ≤ 200 (50–200 guidance); stable `batch_index`; no match in two batches; empty → no batches (FR-011, SC-008).
- **Node selection** (`test_jobs_node_selection.py`) — `select_node` deterministic per domain across calls/processes; distribution; single-node pool (FR-014, SC-005/US3-AS4).
- **Lifecycle** (`test_jobs_lifecycle.py`) — `resolve_finalized_status` boundary values: all-success COMPLETED, mixed PARTIAL_FAILED, none-success FAILED, skipped handling, zero-target COMPLETED (FR-019/020, SC-007).
- **Counters** (`test_jobs_counters.py`) — `aggregate_counts` GROUP BY → correct `Counts`; finalize writes one UPDATE; `mark_target` never touches job counters (FR-018, SC-004).
- **Service** (`test_jobs_service.py`) — `create_match_job` (1 target, MANUAL/API/requested_by, enqueue once); `create_variant_job` (one target per ACTIVE match, inactive excluded); zero-active → COMPLETED, `total_targets=0`, NO enqueue (FR-006/007/010/020).
- **Dispatch task** (`test_jobs_dispatch_task.py`) — RUNNING+started_at once; one `schedule` per batch with selected node + `batch_index`; duplicate delivery → no second POST; `set_workspace_context` called (FR-011/012/013, SC-003).
- **Stall recovery** (`test_jobs_stall_recovery.py`) — re-dispatch only unprogressed targets past timeout; progressed/locked excluded; window-bucketed key idempotent (FR-015, SC-005).
- **Messaging / boundaries / scoping** (`test_jobs_messaging.py`, `test_import_boundaries.py`, `test_jobs_scoping_guard.py`) — enqueue-by-name routes to the right queue; `app_shared.jobs.*`/`messaging` import no scrapy/twisted/fastapi; API never imports `apps/workers`; unscoped select on the new models flagged (Principle I/II).
- **Router** (`test_jobs_router.py`, dependency-overridden session + fake enqueue) — run-match/run-variant/get/results shapes + status codes; unknown/cross-ws match → 404, no job (FR-006..009, SC-006).

## Migration render (offline, no DB)

```bash
SPECIFY_FEATURE_DIRECTORY=specs/008-jobs-orchestration uv run alembic upgrade head --sql
```

Expect `CREATE TABLE scrape_jobs` (with `unique(workspace_id, id)`), `CREATE TABLE scrape_job_targets` (with `unique(scrape_job_id, match_id)` + the composite FK to `scrape_jobs`), and the RLS statements for both. See contracts/migration-jobs.md.

## Live validation (full-stack host — skip-marked here)

```bash
uv run alembic upgrade head            # applies scrape_jobs + scrape_job_targets + RLS
uv run pytest tests/integration -q     # runs only where Postgres/Redis/Scrapyd are reachable
```

Live scenarios (authored, skip cleanly without infra):
- **Run match** (`test_jobs_run_match_live.py`) — POST `/v1/jobs/run/match/{id}` → job + 1 target (scoped), dispatch enqueued, `schedule.json` carries `workspace_id`/`scrape_job_id`/`match_ids` (US1).
- **Run variant** (`test_jobs_run_variant_live.py`) — variant with active + inactive matches → one target per ACTIVE match, `unique(job,match)` enforced; zero-active → COMPLETED, `total_targets=0`, no dispatch (US2).
- **Counters + finalize** (`test_jobs_counters_finalize_live.py`) — simulate target transitions → counters aggregate; status finalizes COMPLETED/PARTIAL_FAILED/FAILED; `completed_at` set (US3).
- **Isolation** (`test_jobs_isolation_live.py`) — cross-workspace job/target read+write blocked (app + RLS); no-context → 0 rows (SC-006).
- **Dispatch to Scrapyd** (`test_jobs_dispatch_scrapyd_live.py`) — authenticated `schedule.json` per batch; retried dispatch does not double-run (SC-003).
```
