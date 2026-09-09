# Packet PA-4 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: opus   Parallel-safe: no   Depends on: PA-3, PA-6
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w4   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A5 — Target lifecycle truth — STARTED transition, phase timestamps, baseline metrics
Acceptance criteria:
- `load_targets` moves every non-terminal target to `STARTED` with `started_at` set, in the same transaction, and a second load is a no-op - the plan's `tests/unit/test_targets_started_transition.py` passes.
- `app_shared.jobs.targets.mark_target` gains `only_if_status` and the timestamp kwargs and issues one `UPDATE ... SET status='STARTED', started_at=COALESCE(started_at, now()) WHERE status IN (...)`. The existing reaper unit tests stay green.
- One Alembic revision adds `scrape_job_targets.claimed_at, remote_accepted_at, first_network_at, document_received_at, extraction_finished_at, persisted_at` (all `TIMESTAMPTZ NULL`) and `request_attempts.attempt_uuid UUID NOT NULL DEFAULT gen_random_uuid()`, `connect_ms`, `ttfb_ms`, `read_ms`, `extract_ms INT NULL`. Single head preserved.
- `ScrapeResult` (`libs/scrape-core/scrape_core/items.py`) carries `attempt_id`, `first_network_at`, `document_received_at`, `extraction_finished_at`, `connect_ms`, `ttfb_ms`, `read_ms`, `extract_ms`; `pipelines._flush_batch` persists them and sets `persisted_at = now()`.
- The seven new gauges are emitted from `opsmetrics/emit.py` with the plan's exact names and SQL, including `crawmatic_target_phase_p95_seconds{phase=due_to_dispatch|dispatch_to_first_network|first_network_to_persisted}`; `cost_model_drift_ratio` is `NULL` (never 0) when the provider figure is absent.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A5: Target lifecycle truth — STARTED transition, phase timestamps, baseline metrics (deep dive §5, audit Stage A)
>
> **Files:**
> - Modify: `libs/scrape-core/scrape_core/targets.py:470-560` (STARTED at pickup)
> - Modify: `libs/shared/app_shared/jobs/targets.py:79-165` (new kwargs `only_if_status`, timestamp fields)
> - Create: `alembic/versions/<rev>_target_lifecycle_timestamps.py`
> - Modify: `libs/scrape-core/scrape_core/items.py` (`ScrapeResult` gains `attempt_id: uuid.UUID`, `first_network_at`, `document_received_at`, `extraction_finished_at`, `connect_ms`, `ttfb_ms`, `read_ms`, `extract_ms`)
> - Modify: `libs/scrape-core/scrape_core/pipelines.py:328-420` (persist them; set `persisted_at`)
> - Modify: `libs/shared/app_shared/opsmetrics/emit.py`, `libs/shared/app_shared/opsmetrics/rules.py`
> - Test: `tests/unit/test_targets_started_transition.py`, `tests/unit/test_ops_metrics_baseline.py`
>
> **Interfaces:**
> - New columns on `scrape_job_targets`: `claimed_at`, `remote_accepted_at`, `first_network_at`, `document_received_at`, `extraction_finished_at`, `persisted_at` (all `TIMESTAMPTZ NULL`). New columns on `request_attempts`: `attempt_uuid UUID NOT NULL DEFAULT gen_random_uuid()`, `connect_ms`, `ttfb_ms`, `read_ms`, `extract_ms INT NULL`.
> - New metrics (names are the contract for B9/D5): `crawmatic_fresh_unique_matches_24h`, `crawmatic_attempts_per_valid_fresh_24h`, `crawmatic_persistence_failures_1h`, `crawmatic_dispatch_ambiguous_intents`, `crawmatic_queue_oldest_pending_seconds`, `crawmatic_cost_model_drift_ratio`, `crawmatic_target_phase_p95_seconds{phase=due_to_dispatch|dispatch_to_first_network|first_network_to_persisted}`.
>
> - [ ] **Step 1: Failing test** — loading targets for a job moves every non-terminal target to `STARTED` with `started_at` set, in the same transaction, and a second load is a no-op:
>
> ```python
> def test_load_targets_marks_started(db_session, seeded_job):
>     from scrape_core.targets import load_targets
>     ids = [m.id for m in seeded_job.matches]
>     load_targets(workspace_id=seeded_job.workspace_id, scrape_job_id=seeded_job.id, match_ids=ids)
>     rows = db_session.execute(select(ScrapeJobTarget).where(ScrapeJobTarget.scrape_job_id == seeded_job.id)).scalars().all()
>     assert {r.status for r in rows} == {ScrapeTargetStatus.STARTED}
>     first = {r.match_id: r.started_at for r in rows}
>     load_targets(workspace_id=seeded_job.workspace_id, scrape_job_id=seeded_job.id, match_ids=ids)
>     assert {r.match_id: r.started_at for r in db_session.execute(select(ScrapeJobTarget)).scalars()} == first
> ```
>
> - [ ] **Step 2: Implement** in `load_targets` after the terminal-status filter: `mark_target(..., status=STARTED, only_if_status=(PENDING, DEFERRED))` which issues one `UPDATE ... SET status='STARTED', started_at=COALESCE(started_at, now()) WHERE status IN (...)`. The reaper (`reaper.py:96`) now has rows to act on; its unit tests stay green.
> - [ ] **Step 3: Timestamps** — spider records `first_network_at` from the downloader (`request.meta["download_slot_start"]` for HTTP; Playwright `request.timing["requestStart"]` for browser), `document_received_at` on response, `extraction_finished_at` after `extract`; the pipeline sets `persisted_at = now()` in `_flush_batch`; `tasks_jobs.dispatch_job` sets `claimed_at` when the intent is planned and `remote_accepted_at` on confirm (B2 keeps these).
> - [ ] **Step 4: Metrics** — SQL for each new gauge in `opsmetrics/emit.py`: `fresh_unique_matches_24h = COUNT(DISTINCT match_id) FROM match_current_prices WHERE updated_at > now()-interval '24h'`; `attempts_per_valid_fresh = attempts_24h / NULLIF(fresh_unique_matches_24h,0)`; `dispatch_ambiguous_intents = COUNT(*) FROM dispatch_intents WHERE state='POSTED' AND updated_at < now()-interval '5 min'`; `cost_model_drift_ratio = ledger_bytes_24h / provider_bytes_24h` (provider figure from the table `import_dataimpulse_usage.py` fills; `NULL` when absent, never 0).
> - [ ] **Step 5:** tests green; single head; commit `feat(jobs): targets really become STARTED; per-phase timestamps; baseline freshness/amplification metrics (deep dive §5)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A5.md

## Scope
Files/areas in scope:
- **A5** — Modify `libs/scrape-core/scrape_core/targets.py` (~`:470-560`), `libs/shared/app_shared/jobs/targets.py` (~`:79-165`), `libs/scrape-core/scrape_core/items.py`, `libs/scrape-core/scrape_core/pipelines.py` (~`:328-420`), `libs/shared/app_shared/opsmetrics/emit.py`, `libs/shared/app_shared/opsmetrics/rules.py`. Create `alembic/versions/<rev>_target_lifecycle_timestamps.py`, `tests/unit/test_targets_started_transition.py`, `tests/unit/test_ops_metrics_baseline.py`.

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
- **A5** — `jobs/targets.py` is 197 lines (`mark_target` at `:79`), `scrape_core/targets.py` 1864 lines, `pipelines.py` 1035 lines with `_flush_batch` in the cited region. Verified fact from the plan's corrections table: NO production code path ever transitions a target to `STARTED` today - this task creates that transition, which is what makes the 2026-09-03 reaper and the D2 deploy-survival proof meaningful.

Task-specific notes:
- **A5** — The metric NAMES are a contract consumed by B9 and D5 - do not rename them.
- **A5** — `pipelines.py`, `items.py` and `opsmetrics/emit.py` are shared with A7 (this stage) and B1/C5 (later stages). No sibling packet touching them runs concurrently; keep edits inside the cited ranges.

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

