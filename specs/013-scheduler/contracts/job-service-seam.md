# Contract: Job-Service Seam (scope→match resolution + scoped job creation)

New shared surface in `libs/shared/app_shared/jobs/`, reused by the scheduler now and by future
manual scope-run endpoints (FR-010/011). Scraping-free; sync SQLAlchemy `Session`; flush-not-commit
(caller owns the transaction). Existing `create_match_job` / `create_variant_job` are unchanged.

## `app_shared/jobs/scopes.py`

```python
def resolve_scope_matches(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    scope: ScrapeScope,
    target_id: uuid.UUID | None,
) -> list[CompetitorProductMatch]: ...
```
Returns the ACTIVE matches for the scope, always via
`scoped_select(CompetitorProductMatch, workspace_id).where(status == MatchStatus.ACTIVE, <scope predicate>)`.

Scope predicates (research R4):

| scope | requires target_id | predicate |
|---|---|---|
| WORKSPACE | no | — |
| COMPETITOR | yes | `M.competitor_id == target_id` |
| PRODUCT | yes | `M.product_id == target_id` |
| VARIANT | yes | `M.product_variant_id == target_id` |
| MATCH | yes | `M.id == target_id` |
| PRODUCT_GROUP | yes | `EXISTS(product_group_items PGI WHERE PGI.product_group_id == target_id AND (PGI.product_id == M.product_id OR PGI.product_variant_id == M.product_variant_id))` |

A missing/dangling target id naturally yields `[]` (no crash — FR-020 / spec Edge Cases).
Pure query logic → unit-testable (with a session/fixture); the per-scope branch selection is
unit-testable without a DB.

## `app_shared/jobs/service.py` — new entry point

```python
def create_scope_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    scope: ScrapeScope,
    target_id: uuid.UUID | None,
    requested_by: uuid.UUID | None,
    job_type: ScrapeJobType = ScrapeJobType.MANUAL,
    source: ScrapeJobSource = ScrapeJobSource.API,
) -> tuple[uuid.UUID | None, ScrapeJobStatus | None]: ...
```
Behavior:
1. `matches = resolve_scope_matches(session, workspace_id=..., scope=..., target_id=...)`.
2. If `matches` is empty → return `(None, None)` (no job, no dispatch — FR-015).
3. Else create one `ScrapeJob(type=job_type, source=source, scope=scope, status=PENDING,
   total_targets=len(matches), workspace_id=..., requested_by=...)`, `flush`; then one
   `ScrapeJobTarget(status=PENDING)` per match; then `_enqueue_dispatch(job.id, workspace_id)`
   (enqueue-before-commit — the service never commits).
4. Return `(job.id, ScrapeJobStatus.PENDING)`.

The scheduler calls it with `job_type=SCHEDULED, source=SCHEDULER`. The manual API run-flows
(current match/variant; future workspace/competitor/product/group) can adopt the same seam.

## Reused, unchanged

- `_enqueue_dispatch` / `SCRAPE_DISPATCH_JOB` (`scrape_dispatch.dispatch_job`, `scrape_dispatch`
  queue) — the existing best-effort Celery `send_task`.
- Idempotent dispatch guard `dispatch_key(scrape_job_id, batch_index)` (`app_shared/scrapyd/client.py`).
- `batching.plan_batches`, `nodes.select_node`, `targets.mark_target`, `lifecycle.*` — untouched;
  they run in the downstream dispatch worker exactly as for manual jobs.

## Enum reuse

`ScrapeScope` (already: WORKSPACE/COMPETITOR/PRODUCT/VARIANT/PRODUCT_GROUP/MATCH),
`ScrapeJobType.SCHEDULED`, `ScrapeJobSource.SCHEDULER`, `MatchStatus.ACTIVE` — all present in
`app_shared.enums`; no enum change.
