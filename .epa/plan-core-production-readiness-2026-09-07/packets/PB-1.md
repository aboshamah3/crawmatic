# Packet PB-1 — Phase B: Stage B — Durable work, reliable schedules
Model: opus   Parallel-safe: yes (with PB-8)   Depends on: PA-8
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B1 — Durable result spool with idempotent persistence and backpressure (F05)
Acceptance criteria:
- `libs/scrape-core/scrape_core/result_spool.py` provides `ResultSpool(path)` wrapping `app_shared.netledger.buffer.DurableEventBuffer` (SQLite WAL, already in the stack) with `append_batch`, `pending(limit)`, `resolve(ids)`, `defer(id, error)`, `pending_count()` and `SPOOL_SCHEMA_VERSION = 1`.
- `process_item` appends to the spool BEFORE the in-memory buffer (durable first). `_flush` passes spool row ids to `_flush_batch(workspace_id, batch, spool_ids)`, which runs the idempotent inserts in one `workspace_txn` and calls `spool.resolve(ids)` on success.
- `_on_flush_failure` calls `spool.defer(id, error)` and schedules a reactor `callLater` retry per `SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS`; after `SCRAPE_FLUSH_QUARANTINE_AFTER` failures the batch moves to `kind='quarantined'` and `crawmatic_persistence_quarantined_batches` increments.
- `close_spider` waits up to 30 s for pending flushes and leaves the rest in the spool; `open_spider` calls `replay_pending()` first so a restarted container drains leftovers before new work.
- Backpressure: when `len(self._pending) >= max_pending_batches`, `process_item` returns a Deferred resolved on the next successful flush. The plan's two unit tests (`test_flush_failure_defers_and_replays_once`, `test_admission_pauses_when_pending_batches_exceed_cap`) pass.
- One Alembic revision makes `request_attempts.attempt_uuid` `UNIQUE (workspace_id, attempt_uuid, created_at)`, adds `price_observations.attempt_uuid UUID NULL` with `UNIQUE (workspace_id, attempt_uuid, scraped_at)` (partition key included), and backfills existing rows with `gen_random_uuid()`. All `_flush_batch` inserts become `ON CONFLICT DO NOTHING` on those keys. Single head preserved.
- New settings: `SCRAPE_RESULT_SPOOL_PATH`, `SCRAPE_FLUSH_MAX_PENDING_BATCHES = 8`, `SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS = (1, 5, 30, 120, 600)`, `SCRAPE_FLUSH_QUARANTINE_AFTER = 5`; both scrapers' `settings.py` read the spool path.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B1: Durable result spool with idempotent persistence and backpressure (F05)
>
> **Files:**
> - Create: `libs/scrape-core/scrape_core/result_spool.py`
> - Modify: `libs/scrape-core/scrape_core/pipelines.py:328-420` (idempotent writes), `:949-1040` (pipeline lifecycle)
> - Create: `alembic/versions/<rev>_observation_attempt_identity.py`
> - Modify: `libs/shared/app_shared/config.py`
> - Modify: `apps/scrapers-http/price_monitor/settings.py`, `apps/scrapers-browser/price_monitor_browser/settings.py` (spool path)
> - Test: `tests/unit/test_result_spool.py`, `tests/unit/test_pipeline_flush_retry.py`, `tests/integration/test_persistence_replay_no_refetch.py`
>
> **Interfaces:**
> - Produces: `ResultSpool(path: Path)` wrapping `app_shared.netledger.buffer.DurableEventBuffer` (SQLite WAL, already in the stack) with `append_batch(results: list[ScrapeResult]) -> list[int]`, `pending(limit) -> list[SpooledBatch]`, `resolve(ids)`, `defer(id, error)`, `SPOOL_SCHEMA_VERSION = 1` (0.3's manifest reads it).
> - Settings: `SCRAPE_RESULT_SPOOL_PATH: Path` (on the same volume as `NETLEDGER_BUFFER_PATH`), `SCRAPE_FLUSH_MAX_PENDING_BATCHES: int = 8`, `SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS: tuple = (1, 5, 30, 120, 600)`, `SCRAPE_FLUSH_QUARANTINE_AFTER: int = 5`.
> - DB identity: `request_attempts.attempt_uuid` (A5) becomes `UNIQUE (workspace_id, attempt_uuid, created_at)`; `price_observations` gains `attempt_uuid UUID NULL` with `UNIQUE (workspace_id, attempt_uuid, scraped_at)` (partition key included). All `_flush_batch` inserts become `ON CONFLICT DO NOTHING` on those keys; `match_current_prices` upsert already uses the monotonic guard (`_monotonic_conflict_where`).
>
> - [ ] **Step 1: Failing tests**:
>
> ```python
> def test_flush_failure_defers_and_replays_once(tmp_path, monkeypatch):
>     from scrape_core.pipelines import BatchedPersistencePipeline
>     calls = []
>     def flaky_flush(ws, batch, spool_ids):
>         calls.append(len(batch))
>         if len(calls) == 1: raise OperationalError("conn reset", None, None)
>     monkeypatch.setattr("scrape_core.pipelines._flush_batch", flaky_flush)
>     p = BatchedPersistencePipeline(max_items=2, interval_seconds=60, spool_path=tmp_path / "spool.sqlite")
>     spider = FakeSpider(); p.open_spider(spider)
>     p.process_item(make_result(), spider); p.process_item(make_result(), spider)
>     run_reactor_until(lambda: p.spool.pending_count() == 0, timeout=5)
>     assert calls == [2, 2]                      # one failure, one successful replay
>     assert p.spool.pending_count() == 0
>
> def test_admission_pauses_when_pending_batches_exceed_cap(tmp_path, monkeypatch):
>     monkeypatch.setattr("scrape_core.pipelines._flush_batch", lambda ws, b, ids: time.sleep(0.5))
>     p = BatchedPersistencePipeline(max_items=1, interval_seconds=60, spool_path=tmp_path / "s.sqlite", max_pending_batches=2)
>     spider = FakeSpider(); p.open_spider(spider)
>     d = [p.process_item(make_result(), spider) for _ in range(3)]
>     assert isinstance(d[2], Deferred) and not d[2].called   # third item waits
> ```
>
> Integration: `test_persistence_replay_no_refetch.py` — real compose Postgres; insert two results; `docker pause` Postgres before flush; assert the spool has 1 pending batch; unpause; `replay_pending()`; assert exactly one `request_attempts` row per `attempt_uuid`, one observation, and the fake downloader was never called.
>
> - [ ] **Step 2: Run** → FAIL. **Step 3: Implement**: `process_item` appends to the spool **before** the in-memory buffer (durable first); `_flush` passes spool row ids to `_flush_batch(workspace_id, batch, spool_ids)`, which runs the idempotent inserts in one `workspace_txn` and, on success, `spool.resolve(ids)`; `_on_flush_failure` calls `spool.defer(id, error)` and schedules a reactor `callLater` retry per the backoff tuple; after `SCRAPE_FLUSH_QUARANTINE_AFTER` failures the batch moves to `kind="quarantined"` and `crawmatic_persistence_quarantined_batches` increments; `close_spider` waits up to 30 s for pending flushes and leaves the rest in the spool; `open_spider` first calls `replay_pending()` so the next run in that container drains leftovers before new work. Backpressure: when `len(self._pending) >= max_pending_batches`, `process_item` returns a Deferred resolved when a flush completes (Scrapy honours it and stops pulling items → the downloader stalls → admission pauses).
> - [ ] **Step 4: Migration** for the unique keys; backfill `attempt_uuid` with `gen_random_uuid()` for existing rows.
> - [ ] **Step 5:** tests green (unit + integration); commit `feat(persistence): durable result spool, idempotent writes, bounded retry, backpressure (F05)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B1.md

## Scope
Files/areas in scope:
- **B1** — Create `libs/scrape-core/scrape_core/result_spool.py`, `alembic/versions/<rev>_observation_attempt_identity.py`, `tests/unit/test_result_spool.py`, `tests/unit/test_pipeline_flush_retry.py`, `tests/integration/test_persistence_replay_no_refetch.py`. Modify `libs/scrape-core/scrape_core/pipelines.py` (~`:328-420`, `:949-1040`), `libs/shared/app_shared/config.py`, `apps/scrapers-http/price_monitor/settings.py`, `apps/scrapers-browser/price_monitor_browser/settings.py`.

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
- **B1** — `pipelines.py` is 1035 lines; `_flush_batch` and `BatchedPersistencePipeline.__init__` are in the cited ranges and were spot-checked. A5 (Stage A, already landed) added `attempt_uuid` to `request_attempts` and `persisted_at` to `_flush_batch` - build on that, do not re-add it. `match_current_prices` already uses the monotonic guard `_monotonic_conflict_where` at `pipelines.py:191`.

Task-specific notes:
- **B1** — Also update `scripts/write_release_manifest.py` so `scrape_result_spool_version` reports `SPOOL_SCHEMA_VERSION` instead of the `null` 0.3 wrote.
- **B1** — The integration test pauses the compose Postgres (`docker pause`): serialized; skip if a parallel sibling is running and note it in the report. Always unpause in a `finally`.

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

