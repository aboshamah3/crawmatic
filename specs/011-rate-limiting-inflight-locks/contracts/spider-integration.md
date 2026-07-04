# Contract: Spider Integration (acquire / backoff / requeue-cap / release)

**File**: `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (EXTEND — never rebuild)
plus the release seam in `libs/scrape-core/scrape_core/pipelines.py`. Covers FR-001, FR-006,
FR-007, FR-010, FR-011, FR-014, FR-016, FR-017; US1/US2/US3; SC-003, SC-005.

Extends the **existing** SPEC-10 dispatch seam: `start()` and `errback()` already
`await run_in_thread(_prepare_dispatch, …)` off-reactor and yield a `scrapy.Request`, and
`load_targets` already loads the resolved `AccessPolicy` + matched `DomainAccessRule` per
`(competitor_id, url_pattern)` group. SPEC-11 hooks **between** the SPEC-10 access decision and
the actual dispatch.

---

## Per-dispatch sequence (in `start()` and `errback()`, after `_prepare_dispatch` decides to fetch)

For a target whose SPEC-10 decision is "dispatch with `access_method` M":

1. **Resolve effective limits** — `resolve_limits(domain_rule, access_policy, settings)` for the
   target's domain + M (from the already-loaded SPEC-10 objects; no new query — FR-008).
2. **Acquire permission (off-reactor)** —
   `perm = await limiter.acquire_permission(redis, workspace_id, domain, M, limits, settings, sem_token)`.
   - **granted** → go to step 3.
   - **denied** → **backoff**: `delay = perm.wait_hint_seconds + random.uniform(JITTER_MIN, JITTER_MAX)`
     (FR-006); increment this request's `requeue_count` and `cumulative_wait`; if **either** cap
     (`REQUEUE_MAX_ATTEMPTS` / `REQUEUE_MAX_TOTAL_WAIT_SECONDS`) is now exceeded → **overflow**
     (see `overflow-dispatch.md`); else `await deferred_delay(delay)` (non-blocking, FR-007) and
     retry from step 1. No semaphore/lock was taken on a denied permission, so nothing to release.
3. **Acquire match lock (off-reactor)** —
   `grant = await limiter.acquire_lock(redis, workspace_id, match_id, mode, settings)`.
   - **`None` (already held)** → release the semaphore slot taken in step 2
     (`await limiter.release_slot(...)`), mark the target `SKIPPED` / `LOCKED_ALREADY_RUNNING`
     via a terminal `ScrapeResult` (the existing skip-emission shape SPEC-10 uses for
     not-dispatched attempts), and **do not fetch** (FR-014, US2 AS1). No requeue.
   - **granted** → go to step 4.
4. **Dispatch** — build the request via the existing `_request_for(...)`, and stamp
   `request.meta["semaphore_key"]`, `request.meta["semaphore_token"]`,
   `request.meta["match_lock_key"]`, `request.meta["match_lock_token"]` so `parse`/`errback`
   and the pipeline can release them. `yield` the request.

Per-request backoff state (`requeue_count`, `cumulative_wait`) is kept keyed by `match_id` on
the spider instance (mirrors `_targets_by_match_id`), reset per fresh target.

## Semaphore release (on response — `parse` / `errback`)
- The concurrency slot represents an **in-flight fetch**; release it as soon as the response or
  failure returns — `await limiter.release_slot(redis, key=meta["semaphore_key"], token=meta["semaphore_token"])`
  off-reactor. This is distinct from the match lock (released only after **persistence**).
- A denied-status HTTP or a transport failure that triggers a SPEC-10 retry: release the slot
  before the retry re-enters step 1 (the retry re-acquires a fresh slot).

## Match-lock release (after persistence — `scrape_core/pipelines.py`)
- The lock spans fetch→persist (§13), so release happens in the **existing off-reactor batched
  flush** `_flush_batch`, **after** the observation/attempt write commits, once per item that
  carries a `match_lock_token`:
  `release_match_lock(redis, key=item.match_lock_key, token=item.match_lock_token)`.
- `ScrapeResult` gains two optional carry-through fields (`match_lock_key`, `match_lock_token`)
  populated from `response.meta`; they are **not** persisted to any DB row — release-only
  metadata. A missing token (e.g. the SKIPPED/not-dispatched path never acquired a lock) ⇒ no
  release attempted.
- Release errors are logged + swallowed inside the flush (never fail a batch — D3).

## Reactor-safety (SC-005)
- Every limiter/lock call is `await run_in_thread(...)`; every wait is `await deferred_delay(...)`.
- No `time.sleep`, no synchronous Redis on the reactor — grep-verified in the spider and pipeline.

## Non-goals
- No "attach to existing job" dedup (§13 optional) — v1 skips/overflows.
- No browser execution — `PLAYWRIGHT_PROXY` key + 30-min lock TTL only reserved (SPEC-14).
