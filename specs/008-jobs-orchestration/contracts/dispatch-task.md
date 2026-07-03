# Contract: `scrape_dispatch` dispatch task (`apps/workers/app/workers/tasks_jobs.py`)

`dispatch_job` — the `scrape_dispatch`-queue Celery task that expands a job into Scrapyd runs. Thin orchestrator over the pure `app_shared.jobs.batching`/`nodes` logic + the reused SPEC-07 `ScrapydDispatchClient`. Relies on the existing `worker_process_init` fork-safety hook (FR-016).

## `dispatch_job(scrape_job_id, workspace_id)` (queue `scrape_dispatch`)

1. Open a session, `set_workspace_context(session, workspace_id)` (RLS), load the job (`scoped_get`) + its `PENDING` targets (`scoped_select`).
2. If the job is not already terminal: set `status=RUNNING`, `started_at=now` (once).
3. `batches = plan_batches(targets, http_min, http_max)` — group by `(competitor_domain, mode)`; HTTP batch 50–200 (FR-011, SC-008). The competitor domain per target comes from the match's competitor (resolved set-based, not per-target).
4. For each batch: `node = select_node(batch.domain, settings.SCRAPYD_HTTP_URLS)` (deterministic, FR-014); `client.schedule(project="price_monitor", spider="generic_price_spider", workspace_id, scrape_job_id, match_ids=batch.match_ids, mode=batch.mode, batch_index=batch.batch_index, node_url=node)`.
5. The client's Redis `SET NX` on `dispatched:{scrape_job_id}:{batch_index}` makes a duplicate/at-least-once delivery a no-op (no second POST) (FR-013, SC-003).

## Idempotency & determinism

- Duplicate delivery of `dispatch_job` for the same job re-plans the **same** batches (deterministic `batch_index`) and re-attempts `schedule` — each `schedule` is neutralized by the persisted-jobid guard, so no batch double-runs.
- `select_node` is a stable hash-by-domain, so a batch always resolves to the same node across retries (FR-014, US3-AS4).
- The task never starts Scrapy in-process (Principle V) — it only calls `schedule.json`.

## Client extension

- `ScrapydDispatchClient.schedule(..., node_url: str | None = None)` — POST to `node_url` (or `SCRAPYD_HTTP_URLS[0]` when `None`, back-compat with the SPEC-07 thin `dispatch_generic_price_spider` task). Auth + the claim/commit/release idempotency ordering are unchanged.

## Tests

- Unit (`test_jobs_dispatch_task.py`, fake client + fake redis + fake session): sets RUNNING+started_at once; one `schedule` per planned batch with the selected node + `batch_index`; a duplicate delivery issues no second POST; `set_workspace_context` invoked before any query.
- Live (`test_jobs_dispatch_scrapyd_live.py`): authenticated `schedule.json` per batch carrying `workspace_id`/`scrape_job_id`/`match_ids`; retried dispatch does not double-run.
