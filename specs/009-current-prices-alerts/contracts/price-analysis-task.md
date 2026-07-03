# Contract: `price_analysis` Celery task + queue + dedup

## Task name & queue

- `app_shared/task_names.py`: `PRICE_ANALYSIS_RECOMPUTE = "price_analysis.recompute_variant"`.
- `apps/workers/app/workers/celery_app.py`:
  - add `"price_analysis": {}` to `app.conf.task_queues`.
  - add `PRICE_ANALYSIS_RECOMPUTE: {"queue": "price_analysis"}` to `app.conf.task_routes`.
  - add `"app.workers.tasks_analysis"` to the `include=[...]` list.
- New module `apps/workers/app/workers/tasks_analysis.py` defines
  `@app.task(name=PRICE_ANALYSIS_RECOMPUTE) def recompute_variant(...)`.

The queue is **separate from `scrape_dispatch`/`maintenance`** and from the Scrapyd/reactor
runtime (Principle V, §26): analysis never runs inside the spider.

## Signature (kwargs, JSON-serializable)

```python
recompute_variant(
    *, workspace_id: str, product_variant_id: str,
    product_id: str | None = None, scrape_job_id: str | None = None,
) -> None
```

## Behavior (idempotent, deterministic)

Within one `get_session()` transaction, after `set_workspace_context(session, workspace_id)`:

1. **Load client price/currency** — `scoped_get(ProductVariant, product_variant_id,
   workspace_id)`. Missing variant → no-op return (defensive; a deleted variant). Read
   `current_price`, `currency`.
2. **Load competitor rows** — `scoped_select(MatchCurrentPrice, ws).where(
   MatchCurrentPrice.product_variant_id == variant_id)` → list of `CompetitorPrice`.
3. **Run the pure engine** — `outcome = engine.analyze(client_price, currency, rows)`.
4. **Currency-mismatch write-back** — for each id in `outcome.mismatched_match_ids`, scoped
   `UPDATE match_current_prices SET comparable=false, error_code='CURRENCY_MISMATCH'` (only
   flips rows currently comparable; idempotent — a second run is a no-op).
5. **Upsert `variant_price_states`** — `pg_insert(...).on_conflict_do_update(
   index_elements=["workspace_id","product_variant_id"], set_={benchmarks,
   comparable_competitor_count, latest_alert_type, latest_alert_severity, calculated_at=now,
   updated_at=now, latest_alert_state_id})`. Deterministic body (only `now()` timestamps
   differ between runs; state fields identical for identical inputs — SC-001).
6. **Read prior alert state** — `scoped_get(VariantAlertState, unique(ws, variant))` → gives
   `prev_type, prev_severity, had_history` (had_history = a row exists).
7. **Compute transition** — `ev = engine.transition(prev_type, prev_severity, outcome.type,
   outcome.severity, had_history=had_history)`.
8. **Upsert `variant_alert_states`**:
   - `status = ACTIVE` if `outcome.type` non-NORMAL else `RESOLVED`.
   - `first_seen_at`: keep existing on UPDATED/UNCHANGED; set `now` on CREATED/REOPENED.
   - `last_seen_at = now` always.
   - `resolved_at = now` on RESOLVED; `NULL` on REOPENED; unchanged otherwise.
   - write `type, severity, client_price, benchmark_price,
     cheapest/average_competitor_price, message, details, updated_at=now`.
9. **Write event only when `ev is not None`** — insert one `price_alert_events` row
   (`event_type=ev`, `previous_type/new_type`, `previous_severity/new_severity`, `message`,
   `details`, `created_at=now`, `alert_state_id`=the alert state id). `ev is None`
   (no-event / UNCHANGED) writes **no** event.
10. `session.commit()`.

## Idempotency (FR-014, SC-001, SC-007)

- Re-running with unchanged `match_current_prices` + variant price yields an equal engine
  `AlertOutcome` ⇒ upserts write the same state (only `updated_at`/`calculated_at` advance)
  and `transition` returns `None` (prev == new) ⇒ **no duplicate event**.
- Therefore at-least-once Celery delivery is safe; no distributed lock needed. The alert
  state's `last_seen_at` advancing on an unchanged re-run is the only visible effect.

## Dedup per variant per job (emission side — see recompute-triggers.md)

The task does **not** itself dedup; the *emitter* (scrape-completion path) claims a Redis
`SET NX` key `analysis:enqueued:{scrape_job_id}:{product_variant_id}` and enqueues only on a
win, collapsing many match completions of one variant in one job into a single recompute
(§26, Principle VIII — no hot-row contention).

## Fork-safety / DB

- Reuses the SPEC-01/08 `worker_process_init` engine-dispose hook (already in
  `celery_app.py`) — the task is simply another DB-touching task on it.
- All reads via `scoped_select`/`scoped_get`; RLS is the second isolation layer.

## Acceptance (skip-clean integration + pure unit)

- Given a seeded variant + comparable `match_current_prices`, the task writes the expected
  `variant_price_states` benchmarks/count/type and `variant_alert_states`; a mismatched-
  currency competitor is flipped `comparable=false`/`CURRENCY_MISMATCH` and excluded.
- Driving NORMAL → HIGH_PRICE → NORMAL yields exactly one CREATED, one RESOLVED (and a later
  REOPENED on re-departure); an unchanged re-run yields zero new events and an advanced
  `last_seen_at`.
- The pure engine ↔ task boundary is exercised by unit tests on the engine and skip-clean
  integration tests on the task.
