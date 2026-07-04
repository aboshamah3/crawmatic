# Quickstart & Validation: Distributed Rate Limiting & In-Flight Locks

Validation guide for SPEC-11. Correctness lives in the **pure Lua limiter/lock logic**
(unit-testable without the reactor); the reactor seam, spider integration, and overflow are
covered by **skip-clean integration tests** (no Docker daemon in this build env — live
Redis/Postgres/Scrapyd checks SKIP, they never fake success). See `contracts/` for exact
signatures and `data-model.md` for keys/enums.

## Prerequisites
- `uv sync --all-packages` (plain `uv sync` wipes workspace member deps — project rule).
- Unit tests need no infra. Integration tests skip cleanly when `REDIS_URL` / DB / Scrapyd are absent.

## Setup / build
```bash
cd /srv/crawmatic/crawmatic
uv sync --all-packages
uv run ruff check libs/shared/app_shared/limiter libs/scrape-core/scrape_core apps/scrapers
uv run pytest -q            # full suite (integration cases SKIP without infra)
```

## Scenario 1 — Per-domain rate limit holds across N acquirers (US1, SC-001)
1. Acquire `capacity+ K` tokens on one `rate:{ws}:{domain}:DIRECT_HTTP` key in a tight loop
   (or from K threads) within one window.
2. **Expect**: exactly `capacity` grants; every further call `granted=False` with
   `wait_hint_seconds > 0`. Two different `workspace_id`s on the same domain do **not** contend.
- Test: `tests/unit/test_rate_limiter.py::test_bucket_bounds_grants`,
  `::test_wait_hint_positive_on_denial`, `::test_workspace_namespacing`.

## Scenario 2 — Concurrency semaphore + crash reclaim (US1 AS2/AS4, SC-004)
1. Acquire `limit` slots on `semaphore:{ws}:{domain}:{m}`; the `limit+1`-th returns `False`.
2. Do **not** release; advance past `SEMAPHORE_SLOT_TTL_SECONDS` (or set a member score in the
   past). Next acquire purges the expired holder and grants — no reaper, no deadlock.
- Test: `tests/unit/test_rate_limiter.py::test_semaphore_cap`, `::test_semaphore_ttl_reclaim`.

## Scenario 3 — Match lock: exactly one owner, fencing release (US2, SC-002)
1. Two `acquire_match_lock` on `lock:scrape:{ws}:{match}` ⇒ exactly one `True`.
2. `release_match_lock(token=other)` ⇒ `0` (no delete). `release_match_lock(token=owner)` ⇒ `1`.
3. After release (or TTL lapse) the key is re-acquirable.
- Test: `tests/unit/test_match_lock.py::test_single_owner`,
  `::test_fencing_compare_and_delete`, `::test_reacquire_after_release`.

## Scenario 4 — Denied request backs off non-blocking, then overflows (US3, SC-003)
1. Force the limiter to keep denying; drive the spider dispatch seam.
2. **Expect**: `deferred_delay(wait_hint + jitter∈[2,20])` between retries (non-blocking — the
   reactor stays responsive); after `REQUEUE_MAX_ATTEMPTS` **or** `REQUEUE_MAX_TOTAL_WAIT_SECONDS`
   the target is marked `DEFERRED` (error_code `RATE_LIMITED`) and `enqueue(SCRAPE_DISPATCH_JOB)`
   is called once; **no** request is yielded for it (slot freed).
- Test (skip-clean): `tests/integration/test_spider_overflow.py`.

## Scenario 5 — Lock collision → SKIPPED / LOCKED_ALREADY_RUNNING (US2 AS1, US4 AS2)
1. Pre-hold `lock:scrape:{ws}:{match}`; drive a second dispatch for the same match.
2. **Expect**: no fetch; the target is `SKIPPED` with `LOCKED_ALREADY_RUNNING`; the semaphore
   slot taken for that attempt is released.
- Test (skip-clean): `tests/integration/test_spider_lock_collision.py`.

## Scenario 6 — Redis unreachable ⇒ fail-closed (FR-023, SC-001)
1. Point the client at a dead Redis (or inject an error).
2. **Expect**: `acquire_token`/`acquire_slot`/`acquire_match_lock` all return not-granted /
   `False` — the request is **not** fetched (backs off / defers). Release swallows the error.
- Test: `tests/unit/test_rate_limiter.py::test_fail_closed_on_redis_error`,
  `tests/unit/test_match_lock.py::test_fail_closed_on_redis_error`.

## Scenario 7 — Reactor never blocked (SC-005) & unique constraint (FR-015)
- Grep assertion: no `time.sleep` and no sync `redis`/`EVAL` outside `run_in_thread` in the
  spider/pipeline scrape path — `tests/unit/test_reactor_safety_grep.py`.
- Verify `uq_scrape_job_targets_scrape_job_id_match_id` exists (skip-clean migration/introspect
  test) — `tests/integration/test_target_unique_constraint.py`.

## Expected outcomes (map to Success Criteria)
| Scenario | Success Criterion |
|---|---|
| 1 | SC-001 (0 rate breaches) |
| 2 | SC-004 (TTL reclaim, 0 deadlocks) |
| 3, 5 | SC-002 (0 duplicate observations) |
| 4 | SC-003 (100% over-cap overflowed, slot freed) |
| 6 | SC-001 + FR-023 (never fetch uncontrolled) |
| 7 | SC-005 (reactor never blocked) + FR-015 |
| 4, 5 | SC-006 (100% events carry a structured code) |
