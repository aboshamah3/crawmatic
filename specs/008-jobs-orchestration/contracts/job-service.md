# Contract: job-creation service (`app_shared.jobs.service`)

Pure-ish orchestration (SQLAlchemy + the `app_shared.messaging` enqueue seam; no scrapy/twisted/fastapi). The API router delegates here so creation logic is unit-testable against a fake session + fake enqueue.

## `create_match_job(session, *, workspace_id, match, requested_by) -> (job_id, status)`

- Precondition: `match` already resolved in-workspace by the router (`scoped_get`).
- Create `ScrapeJob(scope=MATCH, type=MANUAL, source=API, requested_by, match_id=match.id, product_variant_id, product_id, competitor_id, status=PENDING, total_targets=1)` + one `ScrapeJobTarget(status=PENDING, match_id=match.id)`.
- `enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"scrape_job_id": str(job.id), "workspace_id": str(workspace_id)})`.
- Return `(job.id, PENDING)`.

## `create_variant_job(session, *, workspace_id, variant, requested_by) -> (job_id, status)`

- Resolve all **ACTIVE** matches of `variant` via one `scoped_select` (`product_variant_id == variant.id, status == MatchStatus.ACTIVE`). Inactive excluded (US2-AS2).
- Create `ScrapeJob(scope=VARIANT, type=MANUAL, source=API, requested_by, product_variant_id=variant.id, product_id=variant.product_id, status=PENDING, total_targets=N)`.
- **N == 0** (zero active matches): set `status=COMPLETED`, `total_targets=0`, `completed_at=now`, **do not enqueue**; return `(job.id, COMPLETED)` (FR-020, US2-AS4).
- **N > 0**: set-based insert of one `ScrapeJobTarget` per active match (`unique(scrape_job_id, match_id)` guards duplicates), enqueue dispatch, return `(job.id, PENDING)`.

## Rules

- Every read/write is workspace-scoped (`scoped_select`/`scoped_get`); the session already has RLS context set by the router.
- Counters start at 0 and are only ever set by `aggregate_counts` — the service never increments them.
- The service does not call Scrapyd (that is the dispatch task) — it only creates rows + enqueues.

## Tests (`test_jobs_service.py`, fake session + fake enqueue)

- `create_match_job` → 1 job + 1 target, provenance `MANUAL`/`API`/`requested_by`, enqueue called once with the right name/queue/kwargs.
- `create_variant_job` → one target per ACTIVE match, inactive excluded, `total_targets == N`.
- Zero active matches → job COMPLETED, `total_targets=0`, `completed_at` set, **enqueue NOT called**.
