# Contract: Event creation (taxonomy, payloads, enqueue seams)

Producer helpers: `libs/shared/app_shared/webhooks/payloads.py` (pure builders).
Task: `apps/workers/app/workers/tasks_webhooks.py::create_webhook_event`
(`@app.task(name=CREATE_WEBHOOK_EVENT)`, queue `webhook_events`).
Enqueue seam: `app_shared.messaging.enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs=...)`.

All events: created **after** the source `session.commit()`, fire-and-forget, wrapped in a narrow
`try/except` that logs and continues (never blocks/fails/rolls back the source op — FR-009, SC-005).
`status = "PENDING"`, `delivered_at = null`, no outbound HTTP (FR-010, SC-007).

---

## Task signature

```python
@app.task(name=CREATE_WEBHOOK_EVENT)
def create_webhook_event(
    *,
    workspace_id: str,
    event_type: str,          # a WebhookEventType value
    payload: dict,            # JSON-serializable, size-guarded (< 8 KiB)
    dedup_key: str | None = None,   # best-effort Redis SET NX de-dup (mirrors pipelines.py)
) -> None: ...
```
Opens its own `with get_session() as session:` + `set_workspace_context(session, workspace_id)`,
inserts one `WebhookEvent(workspace_id=..., event_type=..., payload=..., status=PENDING)`, commits.
At-least-once tolerant (FR-009): optional `SET NX dedup_key` collapses Celery retries; duplicates are
acceptable, contradictions are not.

---

## event_type taxonomy (derived from existing enums)

### 1. Alert-state transitions — SPEC-09
Seam: `apps/workers/app/workers/tasks_analysis.py::recompute_variant`, after `session.commit()`
(~line 380), only when `event_type is not None`.
Source enum `AlertEventType` (`app_shared/enums.py:391–403`):

| `AlertEventType` | webhook `event_type` |
|---|---|
| `CREATED` | `price.alert.created` |
| `UPDATED` | `price.alert.updated` |
| `RESOLVED` | `price.alert.resolved` |
| `REOPENED` | `price.alert.reopened` |
| `UNCHANGED` | *(never persisted → no event)* |

Payload:
```json
{
  "product_variant_id": "uuid",
  "product_id": "uuid",
  "alert_state_id": "uuid",
  "previous_type": "NORMAL | null",
  "new_type": "RISK",
  "previous_severity": "LOW | null",
  "new_severity": "HIGH",
  "transition": "CREATED"
}
```
`dedup_key = f"alert:{alert_state_id}:{transition}:{scrape_job_id or 'api'}"`.

### 2. Scrape job status finalization — SPEC-08
Seam: `apps/workers/app/workers/tasks_jobs.py::finalize_jobs`, collect finalized `(job_id, status)`
in the loop, enqueue after the single `session.commit()` (~line 401).
Source enum `ScrapeJobStatus` (`app_shared/enums.py:297–311`) terminal states only:

| `ScrapeJobStatus` | webhook `event_type` |
|---|---|
| `COMPLETED` | `scrape.job.completed` |
| `PARTIAL_FAILED` | `scrape.job.partial_failed` |
| `FAILED` | `scrape.job.failed` |
| `CANCELLED` | *(not produced by SPEC-08 finalize → no event in v1)* |

Payload:
```json
{
  "scrape_job_id": "uuid",
  "status": "COMPLETED",
  "success_count": 120,
  "failure_count": 3,
  "skipped_count": 1,
  "total": 124
}
```
`dedup_key = f"job:{scrape_job_id}:{status}"` (job finalizes once → naturally unique).

### 3. Domain strategy change — SPEC-12
Seams (analyze N1 — the transitions do NOT surface in `flush_stats`; enqueue post-commit at the
two REAL sites, once per genuine `apply_*`==`True` transition):
- `apply_promotion` (`app_shared/strategy/promotion.py:148`, returns `promoted: bool` → status
  `ACTIVE`) and `apply_rediscovery` (`app_shared/strategy/rediscovery.py:435`, returns
  `triggered: bool` → status `DEGRADED`) both fire inside
  `app_shared/strategy/flush.py::flush_profile` (promotion per-method ~L271, rediscovery ~L306).
  `flush_profile` must surface its genuine transitions to its caller; enqueue post-commit in
  `apps/workers/app/workers/tasks_strategy.py::flush_stats` (after its `session.commit()`).
- `apply_rediscovery` ALSO fires in `tasks_strategy.py::light_recheck` (~L665); enqueue post-commit
  there (after its `session.commit()` ~L675) for each `triggered` profile — this DEGRADED path is
  otherwise missed.
Source enum `StrategyStatus` (`app_shared/enums.py:444–460`). Anchors approximate — locate by function.

| Trigger | new `StrategyStatus` | webhook `event_type` |
|---|---|---|
| promotion | `ACTIVE` | `domain.strategy.updated` |
| rediscovery | `DEGRADED` | `domain.strategy.updated` |

Payload:
```json
{
  "strategy_profile_id": "uuid",
  "domain": "example.com",
  "new_status": "ACTIVE",
  "change": "PROMOTED | REDISCOVERY_TRIGGERED",
  "method": "optional-winning-method-or-null"
}
```
`dedup_key = f"strategy:{strategy_profile_id}:{new_status}:{change}"`.

---

## Notes / out of v1 scope

- Master-doc §22 also lists `match.scrape.failed` and `product.comparison.updated`. No wired commit
  seam exists for those among this spec's three sources, so they are **out of v1 scope**. Because
  `event_type` is a free `String(64)` column, they can be added later without a schema change.
- `create_webhook_event` never imports the source domain code; the seams call `enqueue(...)` by task
  **name** (Constitution I dependency boundary).
- Payload builders live in `app_shared` (scraping-free) so both worker seams and any future
  producer share one implementation and the size guard.
