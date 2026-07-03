# Contract: Recompute triggers (FR-015, FR-016, §23, §25)

All three triggers enqueue the same idempotent `PRICE_ANALYSIS_RECOMPUTE` task **by name**
via `app_shared.messaging.enqueue(name, queue="price_analysis", kwargs=...)`. No caller
imports `apps/workers` (Principle I).

## Trigger (a): scrape completion — `libs/scrape-core/scrape_core/pipelines.py` `_flush_batch`

The seam already: upserts `match_current_prices`, terminalizes targets in one transaction,
and **after commit** enqueues `SCRAPE_FINALIZE_JOBS` per distinct affected job. SPEC-09 adds,
**after the same commit** (never before — the row must be durable; never on the reactor
thread mid-transaction):

```python
# distinct affected variants that belong to a job (ad-hoc items with no
# scrape_job_id do not dedup-per-job; enqueue directly).
seen: set[tuple[Any, Any, Any]] = set()
for item in batch:
    key = (item.workspace_id, item.scrape_job_id, item.product_variant_id)
    if key in seen:
        continue
    seen.add(key)
    if item.scrape_job_id is not None:
        redis_key = f"analysis:enqueued:{item.scrape_job_id}:{item.product_variant_id}"
        if not get_redis_client().set(redis_key, "1", nx=True, ex=ANALYSIS_DEDUP_TTL):
            continue  # another completed match of this variant already enqueued this job
    enqueue(
        PRICE_ANALYSIS_RECOMPUTE, queue="price_analysis",
        kwargs={"workspace_id": str(item.workspace_id),
                "product_variant_id": str(item.product_variant_id),
                "product_id": str(item.product_id),
                "scrape_job_id": None if item.scrape_job_id is None else str(item.scrape_job_id)},
    )
```

- **Dedup per variant per job** (FR-012, §26, VIII): the `SET NX` guard ⇒ many completed
  matches of one variant across one job's flush batches collapse to a single recompute → no
  contention on `variant_price_states`/`variant_alert_states`.
- `ANALYSIS_DEDUP_TTL` a config constant (e.g. a few hours — comfortably longer than a job's
  lifetime; the key is a contention reducer, not a correctness guard, since the task is
  idempotent).
- `scrape-core` already imports `enqueue` and `get_redis_client` (reactor-safe usage is
  **after** commit, off the fetch path) — import-clean preserved.

## Trigger (b): client price/currency change — API

- `apps/api/app/routers/variants.py` `update_variant` (PATCH): after a successful
  `session.flush()`, if `"current_price" in updates or "currency" in updates` (i.e. the
  price/currency actually changed), enqueue `PRICE_ANALYSIS_RECOMPUTE` with
  `scrape_job_id=None`, `product_id=variant.product_id`. Reflected immediately, no scrape
  (FR-016, §25 "Client price update").
- `bulk_upsert_variants`: after the upsert flush, enqueue once per variant whose
  `current_price`/`currency` was inserted or changed. (Simplest correct: enqueue for every
  variant in the batch — idempotent + low volume; or diff against prior values if cheaply
  available.)
- Enqueue via `app_shared.messaging.enqueue` (API already uses this seam for job dispatch).
  API MUST NOT import `apps/workers`.

## Trigger (c): match archived/paused — match status change

- Where a `CompetitorProductMatch` transitions to an archived/paused (non-active) status
  (the matches surface), enqueue `PRICE_ANALYSIS_RECOMPUTE` for that match's
  `product_variant_id` (`scrape_job_id=None`). The comparable set changed, so the variant's
  benchmarks/alert must be recomputed without waiting for a scrape.
- Same by-name enqueue seam.

## Cross-cutting

- **Ordering**: emission always **after** the DB transaction that made the triggering change
  durable (so the task reads committed data).
- **At-least-once safety**: every trigger is safe to fire more than once (task idempotent).
- **No reactor blocking**: trigger (a)'s Redis/enqueue runs post-commit in the pipeline's
  already-off-reactor flush continuation (same place `SCRAPE_FINALIZE_JOBS` is enqueued).

## Acceptance

- N completed matches of one variant in one job ⇒ exactly one `PRICE_ANALYSIS_RECOMPUTE`
  enqueued for that variant/job (SC-007) — asserted with a fake Redis honoring `SET NX`.
- A PATCH that changes `price`/`currency` enqueues one recompute; a PATCH that changes only
  `title` enqueues none.
- An archived match enqueues a recompute for its variant.
