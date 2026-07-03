# Contract: Jobs run/status endpoints (`apps/api/app/routers/jobs.py`)

Four `/v1` endpoints on the SPEC-03 auth seam (`app.deps.get_current_principal` → `set_workspace_context` already applied to the yielded session), scope-gated via `app.deps.require_scopes(...)`, all reads/writes through `app_shared.repository.scoped_select`/`scoped_get` with RLS as the second isolation layer. Job creation delegates to `app_shared.jobs.service`; dispatch is enqueued through `app_shared.messaging` (never importing `apps/workers`). New scopes `jobs:read` / `jobs:write` (following the `matches:*` precedent).

## `POST /v1/jobs/run/match/{match_id}` — `jobs:write` (FR-006, US1)

- Resolve the match via `scoped_get(session, CompetitorProductMatch, match_id, ws)`. Unknown / cross-workspace → **404 `NOT_FOUND`**, **no job created** (US1-AS4, edge: match not found).
- Create a `ScrapeJob` (`scope=MATCH`, `type=MANUAL`, `source=API`, `requested_by=principal.id`, `match_id`/`product_variant_id`/`product_id`/`competitor_id` from the match, `status=PENDING`, `total_targets=1`) + exactly **one** `ScrapeJobTarget` (`status=PENDING`), enqueue `scrape_dispatch.dispatch_job`, return the job id.
- Response: **202** `JobRunResponse { id, status }` (`status=PENDING`).

## `POST /v1/jobs/run/variant/{variant_id}` — `jobs:write` (FR-007, US2)

- Resolve the variant via `scoped_get(session, ProductVariant, variant_id, ws)`. Unknown / cross-workspace → **404 `NOT_FOUND`**, no job.
- Find all **ACTIVE** matches of the variant (`scoped_select(CompetitorProductMatch, ws).where(product_variant_id == variant_id, status == ACTIVE)`); inactive matches excluded (US2-AS2).
- Create one `ScrapeJob` (`scope=VARIANT`, `product_variant_id`, `product_id`, `type=MANUAL`, `source=API`, `requested_by`), `total_targets = N`, one **unique** `ScrapeJobTarget` per active match (set-based insert; `unique(scrape_job_id, match_id)`), enqueue dispatch, return the job id (**202**, `status=PENDING`).
- **Zero active matches** → create the job, `total_targets=0`, finalize **COMPLETED** immediately, **no** dispatch; response **202** `JobRunResponse { id, status=COMPLETED }` (US2-AS4, FR-020).

## `GET /v1/jobs/{job_id}` — `jobs:read` (FR-008, US1-AS3)

- `scoped_get(session, ScrapeJob, job_id, ws)`; miss → **404 `NOT_FOUND`**.
- Response **200** `JobResponse { id, type, scope, status, priority, total_targets, success_count, failure_count, skipped_count, requested_by, source, started_at, completed_at, created_at }` (counts as last aggregated/finalized).

## `GET /v1/jobs/{job_id}/results` — `jobs:read` (FR-009, US1-AS3)

- Verify the job is visible (`scoped_get` ScrapeJob); miss → **404**.
- `scoped_select(ScrapeJobTarget, ws).where(scrape_job_id == job_id)`.
- Response **200** `JobResultsResponse { items: [ JobTargetResponse { id, match_id, status, error_code, started_at, completed_at, locked_at } ] }`. Bounded by the job's target count (no cursor for the in-scope endpoints; the paginated `GET /v1/jobs` list is a later spec).

## Isolation (Principle II, edge: cross-workspace)

- Every endpoint is workspace-scoped through the auth seam + `scoped_*` + RLS. A caller can never create/view/affect a job or target outside their workspace; a read with no workspace context yields zero rows (RLS fail-closed). Cross-workspace `job_id`/`match_id`/`variant_id` behave as not-found.

## Tests

- Unit (`test_jobs_router.py`, dependency-overridden session + fake `enqueue`): run-match → 202 + 1 target + `MANUAL`/`API`/`requested_by`, enqueue called once; unknown/cross-ws match → 404, no job, no enqueue; run-variant → one target per active match, inactive excluded; zero-active → 202 COMPLETED, **no** enqueue; get/results shapes; missing job → 404.
- Live (`test_jobs_run_match_live.py`, `test_jobs_run_variant_live.py`, `test_jobs_isolation_live.py`): end-to-end creation + dispatch enqueue; `unique(scrape_job_id, match_id)`; cross-workspace + no-context (0 rows).
