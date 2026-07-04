---
description: "Task list for SPEC-11 — Distributed Rate Limiting & In-Flight Locks"
---

# Tasks: Distributed Rate Limiting & In-Flight Locks

**Input**: Design documents from `/specs/011-rate-limiting-inflight-locks/`

**Prerequisites**: plan.md (required), spec.md (user stories), research.md, data-model.md, contracts/ (rate-limiter, match-lock, reactor-seam, spider-integration, overflow-dispatch, observability)

**Tests**: INCLUDED. quickstart.md names the exact test modules and each user story has an explicit Independent Test, so test tasks are authored. Per project memory (no Docker daemon in this build env), pure Lua / limit-resolution / key-format / jitter logic runs as **unit tests that actually pass**; anything needing a live Redis/Postgres/Scrapyd is authored as an **integration test that SKIPS cleanly** when the service is absent (SPEC-07..10 convention).

**Organization**: Grouped by user story (US1 P1, US2 P1, US3 P2, US4 P3) for independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: `[US1]`..`[US4]` — user story this task serves (Setup/Foundational/Polish carry no story tag)
- Every task carries an exact repo-relative file path

## Path & build conventions

- **uv workspace**: dependency sync is `uv sync --all-packages` (plain `uv sync` wipes workspace member deps — project rule); tests run with `uv run pytest`.
- Pure Redis logic → `libs/shared/app_shared/limiter/` (stdlib + injected `redis.Redis` client; no Scrapy/Twisted/FastAPI import — sibling of `app_shared/access/budget.py`).
- Reactor seam (only place allowed to touch Twisted) → `libs/scrape-core/scrape_core/limiter.py` + `libs/scrape-core/scrape_core/reactor.py`.
- Spider integration → `apps/scrapers/price_monitor/spiders/generic_price_spider.py`; pipeline release → `libs/scrape-core/scrape_core/pipelines.py`.
- Overflow re-dispatch → `apps/workers/app/workers/tasks_jobs.py`.
- Tests live at repo root: `tests/unit/`, `tests/integration/` (existing layout).
- **Schema**: the ONLY schema-adjacent change is adding `ScrapeTargetStatus.DEFERRED` (VARCHAR-rendered `StrEnum` member — NO Alembic migration, NO new table/column). Error codes `RATE_LIMITED` / `LOCKED_ALREADY_RUNNING` and `unique(scrape_job_id, match_id)` already exist and are reused/verified, not recreated.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Sync the workspace and scaffold the new package so all later phases have a home.

- [X] T001 Sync workspace dependencies from repo root: `uv sync --all-packages` (NEVER plain `uv sync` — it wipes workspace member deps). No new third-party dependency is added; confirm `redis`, `scrapy`, `twisted`, `sqlalchemy`, `celery` resolve.
- [X] T002 [P] Create the new pure-logic package `libs/shared/app_shared/limiter/__init__.py` (empty package marker; re-exports added as modules land).

**Checkpoint**: `uv run pytest -q` collects cleanly; new package importable.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Enum member, tuning knobs, key builders, and the reactor-seam module skeleton that every user story depends on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Add `DEFERRED = "DEFERRED"` to `ScrapeTargetStatus` in `libs/shared/app_shared/enums.py` (VARCHAR-rendered `StrEnum` — no migration). Do NOT add it to any `_TERMINAL_TARGET_STATUSES` set — DEFERRED is non-terminal (per data-model.md §2.1, overflow-dispatch.md §1). Update the class docstring to mention the DEFERRED (overflow → re-dispatch) transition.
- [X] T004 [P] Add the 10 env-tunable `Settings` knobs to `libs/shared/app_shared/config.py` with defaults from observability.md: `RATE_LIMIT_DEFAULT_PER_MINUTE=60`, `RATE_LIMIT_DEFAULT_CONCURRENCY=4`, `RATE_LIMIT_KEY_TTL_SLACK_SECONDS=120`, `SEMAPHORE_SLOT_TTL_SECONDS=600`, `MATCH_LOCK_HTTP_TTL_SECONDS=600`, `MATCH_LOCK_BROWSER_TTL_SECONDS=1800`, `REQUEUE_MAX_ATTEMPTS=5`, `REQUEUE_MAX_TOTAL_WAIT_SECONDS=300`, `RATE_LIMIT_JITTER_MIN_SECONDS=2`, `RATE_LIMIT_JITTER_MAX_SECONDS=20`.
- [X] T005 [P] Implement workspace-namespaced key builders in `libs/shared/app_shared/limiter/keys.py`: `rate_key(workspace_id, domain, access_method)`, `semaphore_key(...)`, `match_lock_key(workspace_id, match_id)` producing `rate:{ws}:{domain}:{ACCESS_METHOD}` / `semaphore:{ws}:{domain}:{ACCESS_METHOD}` / `lock:scrape:{ws}:{match_id}` (FR-002/003/009/010; `access_method` is the `AccessMethod` value string; `workspace_id` first after the family prefix).
- [X] T006 Create the reactor-seam module `libs/scrape-core/scrape_core/limiter.py` with the **DECISION OF RECORD** module docstring stated verbatim from reactor-seam.md (deferToThread for all Redis round-trips via `scrape_core.db.run_in_thread`; `callLater`-backed Deferred for backoff; no async-redis; no `time.sleep`; no sync Redis on the reactor). Import `run_in_thread`; leave wrapper functions as stubs filled in per story (Constitution V — this decision is owned in scrape-core).
- [X] T007 [P] Implement the non-blocking delay helper in `libs/scrape-core/scrape_core/reactor.py`: `deferred_delay(seconds) -> Deferred` via `d = Deferred(); reactor.callLater(seconds, d.callback, None); return d` (FR-007, SC-005 — never `time.sleep`, never blocks a thread).

**Checkpoint**: Foundation ready — user stories can begin.

---

## Phase 3: User Story 1 — Per-domain rate limits hold across every worker (Priority: P1) 🎯 MVP

**Goal**: A cluster-wide, Redis-backed token bucket + concurrency semaphore that every outbound request must be granted before fetching, so combined rate to a domain (per access method, per workspace) never exceeds the configured limit; denials return a positive wait hint and are rescheduled after a jittered non-blocking delay. Fails **closed** on Redis error.

**Independent Test**: Drive N concurrent `acquire_token` calls against one `rate:` key within a window → grants ≤ capacity, remainder denied with `wait_hint_seconds > 0`; drive semaphore to its cap → over-cap denied, expired holders reclaimed on next acquire; two workspaces on one domain never contend.

### Tests for User Story 1

- [X] T008 [P] [US1] Unit tests in `tests/unit/test_rate_limiter.py`: `test_bucket_bounds_grants` (≤ capacity grants across many acquires in one window), `test_wait_hint_positive_on_denial`, `test_workspace_namespacing` (two workspace_ids never share a bucket), `test_semaphore_cap`, `test_semaphore_ttl_reclaim` (member score in the past is purged on next acquire — no reaper), `test_fail_closed_on_redis_error` (injected Redis error ⇒ `granted=False` / slot `False`). Use a real ephemeral Redis if `REDIS_URL` is set, else a fakeredis/in-memory double; these MUST actually run and pass (covers SC-001, SC-004, FR-023).

### Implementation for User Story 1

- [X] T009 [P] [US1] Implement `EffectiveLimits` dataclass + `resolve_limits(*, domain_rule, access_policy, settings)` in `libs/shared/app_shared/limiter/limits.py`: precedence enabled `DomainAccessRule` override → resolved `AccessPolicy.max_requests_per_minute` → `Settings` defaults; `per_minute`/`concurrency` floored to ≥ 1; `cooldown_seconds` carried through and **consumed** as the post-denial backoff floor in T013 — not a dead value (D4, FR-006/FR-008 — reads already-resolved SPEC-10 objects, no new query/column).
- [X] T010 [US1] Implement the atomic Lua token bucket in `libs/shared/app_shared/limiter/bucket.py`: `AcquireResult(granted, wait_hint_seconds)` + `acquire_token(redis, *, key, capacity, ttl_seconds)`. Register the script once (`register_script`); compute `now` from `redis.call('TIME')` (server clock, FR-004); refill `min(capacity, tokens + (now-ts)*capacity/60)`; grant/decrement or deny with `wait_hint = ceil((1-tokens)*60/capacity)`; `PEXPIRE` on every path; Redis error ⇒ `AcquireResult(granted=False, wait_hint_seconds=default_backoff)` (fail-closed, FR-023).
- [X] T011 [US1] Implement the sorted-set concurrency semaphore in `libs/shared/app_shared/limiter/bucket.py`: `acquire_slot(redis, *, key, limit, token, slot_ttl_seconds, key_ttl_seconds) -> bool` (Lua: `ZREMRANGEBYSCORE key -inf now` purge → `ZCARD < limit` ⇒ `ZADD (now+slot_ttl) token` + `PEXPIRE` ⇒ grant, else deny; acquire Redis error ⇒ `False`, fail-closed) and `release_slot(redis, *, key, token) -> None` (`ZREM`; Redis error logged + swallowed — D3). (FR-003/004/005, SC-004.)
- [X] T012 [US1] Fill the US1 wrappers in `libs/scrape-core/scrape_core/limiter.py`: `async acquire_permission(redis, *, workspace_id, domain, access_method, limits, settings, sem_token) -> Permission` (token bucket THEN semaphore, both `await run_in_thread(...)`; bucket denies ⇒ semaphore untouched; returns `granted`, `wait_hint_seconds`, semaphore `key`+`token`) and `async release_slot(redis, *, key, token)`. Propagate fail-closed as not-granted, never raise (reactor-seam.md).
- [X] T013 [US1] Extend the dispatch seam in `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (`start()` + `errback()`, after `_prepare_dispatch` decides to fetch): call `resolve_limits(...)` from the already-loaded SPEC-10 objects, then `perm = await acquire_permission(...)`; on grant proceed; on denial compute `delay = max(perm.wait_hint_seconds, limits.cooldown_seconds) + random.uniform(JITTER_MIN, JITTER_MAX)` (cooldown floor per FR-006), bump per-request `requeue_count`/`cumulative_wait` (kept keyed by `match_id` on the spider instance, reset per fresh target), `await deferred_delay(delay)` (non-blocking) and retry from limit-resolution. No semaphore/lock taken on a denied permission. (FR-001/006/007; overflow branch is US3.)
- [X] T014 [US1] Add the semaphore release on fetch completion in `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (`parse` + `errback`): `await release_slot(redis, key=meta["semaphore_key"], token=meta["semaphore_token"])` off-reactor as soon as the response/failure returns (slot is held for the fetch only, distinct from the match lock); release the slot before any SPEC-10 retry re-enters. Stamp `semaphore_key`/`semaphore_token` onto `request.meta` at dispatch.

**Checkpoint**: US1 fully functional — cluster-wide rate + concurrency enforcement with non-blocking jittered backoff; unit tests green.

---

## Phase 4: User Story 2 — The same match is never scraped concurrently (Priority: P1)

**Goal**: A spider-owned fencing-token match lock acquired immediately before fetch and released after persistence; a held lock ⇒ target `SKIPPED` / `LOCKED_ALREADY_RUNNING`, no duplicate fetch. Fails closed on Redis error; release is a Lua compare-and-delete.

**Independent Test**: Two `acquire_match_lock` on one key ⇒ exactly one `True`; `release_match_lock(token=other)` ⇒ `0` (no delete), `release_match_lock(token=owner)` ⇒ `1`; key re-acquirable after release/TTL.

### Tests for User Story 2

- [X] T015 [P] [US2] Unit tests in `tests/unit/test_match_lock.py`: `test_single_owner` (two concurrent acquires ⇒ exactly one True), `test_fencing_compare_and_delete` (release with foreign token is a no-op; with owner token deletes), `test_reacquire_after_release`, `test_fail_closed_on_redis_error` (injected error ⇒ `acquire_match_lock` returns `False`). Real ephemeral Redis if available else in-memory double — MUST run and pass (SC-002, FR-023).
- [X] T016 [P] [US2] Unit test in `tests/unit/test_rate_limiter.py` (or a shared `test_limits_invariants.py`): assert the TTL invariant `min(MATCH_LOCK_HTTP_TTL_SECONDS, MATCH_LOCK_BROWSER_TTL_SECONDS) > REQUEUE_MAX_ATTEMPTS × (typical wait_hint + RATE_LIMIT_JITTER_MAX_SECONDS)` and jitter bounds (`JITTER_MIN < JITTER_MAX`, both > 0) hold for the default Settings (FR-013, US2 AS4).
- [X] T017 [P] [US2] Skip-clean integration test `tests/integration/test_spider_lock_collision.py`: pre-hold `lock:scrape:{ws}:{match}`, drive a second dispatch for the same match ⇒ no fetch, target `SKIPPED` + `LOCKED_ALREADY_RUNNING`, the attempt's semaphore slot released. `pytest.skip(...)` cleanly when Redis/Scrapyd absent (SC-002, US2 AS1).
- [X] T018 [P] [US2] Skip-clean integration test `tests/integration/test_target_unique_constraint.py`: introspect/verify `uq_scrape_job_targets_scrape_job_id_match_id` exists on `scrape_job_targets` (FR-015 — verify only, do NOT add). `pytest.skip(...)` when Postgres absent.

### Implementation for User Story 2

- [X] T019 [P] [US2] Implement `libs/shared/app_shared/limiter/locks.py`: `new_fencing_token() -> str` (`secrets.token_hex(16)`), `acquire_match_lock(redis, *, key, token, ttl_seconds) -> bool` (`SET key token NX PX ttl`; NX fail ⇒ `False`; Redis error ⇒ `False`, fail-closed), `release_match_lock(redis, *, key, token) -> bool` (Lua compare-and-delete: DEL only if `GET==token`; Redis error logged + swallowed) (FR-010/012/013/014, D6).
- [X] T020 [P] [US2] Add release-only carry-through fields `match_lock_key: str | None` and `match_lock_token: str | None` to `ScrapeResult` in `libs/scrape-core/scrape_core/items.py` (populated from `response.meta`; NOT persisted to any DB row — spider-integration.md §match-lock release).
- [X] T021 [US2] Fill the US2 wrappers in `libs/scrape-core/scrape_core/limiter.py`: `async acquire_lock(redis, *, workspace_id, match_id, mode, settings) -> LockGrant | None` (build mode-sized TTL — HTTP vs browser, generate fencing token, `await run_in_thread(acquire_match_lock, ...)`, return `key`+`token` or `None` when held) and `async release_lock(redis, *, key, token)`. Fail-closed surfaces as `None` (reactor-seam.md).
- [X] T022 [US2] Extend `apps/scrapers/price_monitor/spiders/generic_price_spider.py` step 3 (after the US1 permission grant): `grant = await acquire_lock(...)`; on `None` (already held) `await release_slot(...)` the step-2 semaphore slot, emit a terminal `SKIPPED` / `LOCKED_ALREADY_RUNNING` `ScrapeResult` (existing skip-emission shape, no fetch, no requeue); on grant stamp `request.meta["match_lock_key"]`/`["match_lock_token"]` and yield the request (FR-011/014, US2 AS1).
- [X] T023 [US2] Extend the batched flush in `libs/scrape-core/scrape_core/pipelines.py` `_flush_batch`: after the observation/attempt write commits, for each item carrying a `match_lock_token` call `release_match_lock(redis, key=item.match_lock_key, token=item.match_lock_token)` in the SAME existing off-reactor flush (no new reactor hop); missing token ⇒ no release; release errors logged + swallowed (FR-011/016, D3, Constitution V).

**Checkpoint**: US1 AND US2 both work independently — exactly-once in-flight fetch per match, fencing-safe release.

---

## Phase 5: User Story 3 — Rate-limited work overflows back to the queue (Priority: P2)

**Goal**: In-spider rescheduling is bounded by requeue count AND cumulative wait; on cap exceed the target is marked `DEFERRED` (carrying `RATE_LIMITED`) and re-dispatched via the existing Celery `scrape_dispatch` producer, freeing the Scrapyd slot immediately; re-dispatch re-enters the full lock+limiter gate.

**Independent Test**: Force the limiter to keep denying ⇒ spider reschedules only up to the cap, then marks the target `DEFERRED` + `enqueue(SCRAPE_DISPATCH_JOB)` exactly once and yields NO request (slot freed); a DEFERRED target is picked up by the next `dispatch_job`.

### Tests for User Story 3

- [X] T024 [P] [US3] Skip-clean integration test `tests/integration/test_spider_overflow.py`: force continuous limiter denial, drive the dispatch seam ⇒ backoff via `deferred_delay` up to `REQUEUE_MAX_ATTEMPTS`/`REQUEUE_MAX_TOTAL_WAIT_SECONDS`, then target marked `DEFERRED` with `RATE_LIMITED`, `enqueue(SCRAPE_DISPATCH_JOB)` called once, no request yielded (slot freed). `pytest.skip(...)` when Redis/Scrapyd absent (SC-003, US3 AS1/AS2).
- [X] T025 [P] [US3] Unit test in `tests/unit/test_overflow_expansion.py`: the `dispatch_job` expansion query (the target-selection query, ~`tasks_jobs.py:212`, NOT the stalled-target reaper at ~:352) selects both `PENDING` and `DEFERRED`; and `mark_target(status=DEFERRED, error_code=RATE_LIMITED)` stamps no `completed_at`/`started_at` and persists the error code. Use a session double / in-memory fixture so it actually runs (FR-018/019).

### Implementation for User Story 3

- [X] T026 [P] [US3] Extend `mark_target` in `libs/shared/app_shared/jobs/targets.py`: (a) accept `status=ScrapeTargetStatus.DEFERRED` — stamp no `completed_at` and no `started_at` (non-terminal); do NOT add DEFERRED to `_TERMINAL_TARGET_STATUSES`. (b) **Broaden `error_code` persistence**: the current code stamps `error_code` ONLY on `status == FAILED`, which silently drops it on `SKIPPED`/`DEFERRED` and breaks FR-020/FR-021/SC-006. Change the gate so `error_code` is written whenever it is provided (`error_code is not None`), regardless of status — covering FAILED (unchanged), SKIPPED→`LOCKED_ALREADY_RUNNING`, and DEFERRED→`RATE_LIMITED`. Update the docstring accordingly. Add/extend a unit test asserting error_code is persisted on all three statuses (FR-020/FR-021, SC-006, overflow-dispatch.md §2, observability.md).
- [X] T027 [US3] Add the overflow branch to the backoff path in `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (US1 T013 denial branch): when `requeue_count > REQUEUE_MAX_ATTEMPTS` OR `cumulative_wait > REQUEUE_MAX_TOTAL_WAIT_SECONDS` → release any held semaphore slot, `await run_in_thread(mark_target_deferred, ...)` (one `workspace_txn` calling `mark_target(status=DEFERRED, error_code=RATE_LIMITED)`), `await run_in_thread(enqueue, SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"scrape_job_id": str(scrape_job_id)})`, and yield NO request. Import `app_shared.messaging` + `app_shared.task_names` only (never `apps/workers` — Principle I) (FR-017/018, SC-003).
- [X] T028 [US3] Update the re-dispatch expansion query in `apps/workers/app/workers/tasks_jobs.py` `dispatch_job` (the expansion/target-selection query at ~line 212): change `ScrapeJobTarget.status == PENDING` to `ScrapeJobTarget.status.in_((ScrapeTargetStatus.PENDING, ScrapeTargetStatus.DEFERRED))` so overflowed targets are re-picked and transition `DEFERRED → STARTED`, re-subject to the lock+limiter gate. Do NOT touch the stalled-target reaper query (~line 352) — it must keep matching only its own status set so DEFERRED is not treated as stalled. Leave `_TERMINAL_JOB_STATUSES` guard unchanged (overflow-loop ceiling) (FR-019, US3 AS3).

**Checkpoint**: US1–US3 functional — no Scrapyd slot held past the cap; overflow bounded by job lifecycle.

---

## Phase 6: User Story 4 — Contention is observable via structured signals (Priority: P3)

**Goal**: Rate-limit overflows and lock collisions carry structured codes (`RATE_LIMITED` / `LOCKED_ALREADY_RUNNING`) on the target/attempt, and contention events emit structured logs/counters (Constitution §31).

**Independent Test**: Trigger a rate-limit overflow and a lock collision ⇒ each persisted outcome carries the correct structured code, distinguishable from other codes.

### Tests for User Story 4

- [X] T029 [P] [US4] Skip-clean integration test `tests/integration/test_observability_codes.py`: assert an overflowed target persists `status=DEFERRED` + `error_code=RATE_LIMITED` and a lock-collision target persists `status=SKIPPED` + `error_code=LOCKED_ALREADY_RUNNING` via the existing `ScrapeResult → mark_target` path (SC-006, US4 AS1/AS2). `pytest.skip(...)` when infra absent.
- [X] T030 [P] [US4] Unit test in `tests/unit/test_observability_logs.py`: capture structured log/counter emission for `rate_limit.hit`, `rate_limit.requeue`, `rate_limit.overflow`, `semaphore.denied`, `dedup.skip`, `dedup.release` with their documented fields (assert keys/fields present) — runs without infra.

### Implementation for User Story 4

- [X] T031 [US4] Emit the structured logs/counters from `apps/scrapers/price_monitor/spiders/generic_price_spider.py`: `rate_limit.hit` (token denied: workspace_id, domain, access_method, wait_hint), `rate_limit.requeue` (each backoff: workspace_id, match_id, requeue_count, delay), `rate_limit.overflow` (cap exceeded: workspace_id, scrape_job_id, match_id), `semaphore.denied`, `dedup.skip` (lock held: workspace_id, match_id) — JSON, namespaced per workspace/domain/access_method (observability.md, Constitution §31, FR-022).
- [X] T032 [US4] Emit `dedup.release` (workspace_id, match_id, released:bool) from the pipeline release in `libs/scrape-core/scrape_core/pipelines.py` `_flush_batch`, and confirm the overflow (`RATE_LIMITED`) and lock-collision (`LOCKED_ALREADY_RUNNING`) codes flow through the `ScrapeResult → mark_target` writer. NOTE: this depends on the T026 error_code broadening — the codes are persisted via that single writer (no *new* persistence path is added, but T026 does change `mark_target`'s stamping gate). Reuses the existing terminal-write path (FR-020/021, SC-006).

**Checkpoint**: All four stories functional; every overflow/collision is code-attributable and logged.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Reactor-safety proof, lint, and full validation.

- [X] T033 [P] Reactor-safety grep test `tests/unit/test_reactor_safety_grep.py`: assert ZERO `time.sleep` and ZERO synchronous `redis`/`EVAL` calls outside a `run_in_thread`/`deferToThread` boundary anywhere in `apps/scrapers/price_monitor/spiders/generic_price_spider.py` and `libs/scrape-core/scrape_core/{limiter,pipelines,reactor}.py` (SC-005, FR-007) — runs without infra. Also assert (FR-016 negative check, analyze C1) that `apps/workers/app/workers/tasks_analysis.py` references NO scrape-lock symbol (`lock:scrape`, `acquire_match_lock`, `release_match_lock`) — price_analysis runs after lock release and must not depend on the scrape lock.
- [X] T034 [P] Add re-exports to `libs/shared/app_shared/limiter/__init__.py` (keys, limits, bucket, locks public API) and confirm `app_shared` imports NO Scrapy/Twisted/FastAPI/`apps.*` (Constitution I).
- [X] T035 Run `uv run ruff check libs/shared/app_shared/limiter libs/scrape-core/scrape_core apps/scrapers apps/workers` and fix findings.
- [X] T036 Run the full suite `uv run pytest -q` from repo root: confirm all unit tests PASS and every infra-dependent integration test SKIPS cleanly (no failures, no fakes) — record the skip list in the feature state per SPEC-07..10 convention.
- [X] T037 Execute the quickstart.md validation walkthrough (Scenarios 1–7) and confirm the mapping to SC-001..SC-006 holds.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup — BLOCKS all user stories (keys, config, enum, reactor seam skeleton, deferred_delay).
- **User Stories (Phases 3–6)**: all depend on Foundational.
  - US1 (P1) and US2 (P1) are independent of each other at the pure-logic layer (`bucket.py`/`limits.py` vs `locks.py`) and can be built in parallel; they converge in the spider file (T013/T014 vs T022), which serializes those specific spider edits.
  - US3 (P2) depends on US1 (extends the US1 backoff branch T013) and on the DEFERRED enum (T003).
  - US4 (P3) is a thin layer over US1–US3 (logs + code-mapping assertions).
- **Polish (Phase 7)**: depends on all targeted stories.

### Within Each User Story

- Tests are authored alongside implementation (unit tests must pass; integration tests skip-clean).
- Pure logic (`limits.py`, `bucket.py`, `locks.py`) before its reactor wrapper (`scrape_core/limiter.py`) before spider/pipeline wiring.

### Parallel Opportunities

- Setup: T002 in parallel with T001's tail.
- Foundational: T004, T005, T007 are `[P]` (distinct files); T003, T006 independent too.
- US1: T008 (tests) and T009 (`limits.py`) are `[P]`; T010/T011 share `bucket.py` (serialize); T012 after T010/T011; T013 after T012; T014 after T013.
- US2: T015, T016, T017, T018 (tests) and T019 (`locks.py`), T020 (`items.py`) are `[P]`; T021 after T019; T022 after T021; T023 after T020/T021.
- US3: T024, T025 `[P]`; T026 `[P]`; T027 after T026 + US1 T013; T028 after T003.
- US4: T029, T030 `[P]`; T031 after US1–US3 spider paths; T032 after US2 T023.
- Polish: T033, T034 `[P]`.

---

## Parallel Example: User Story 1

```bash
# Author the US1 unit tests and the pure limit resolver together (different files):
Task: "Unit tests in tests/unit/test_rate_limiter.py"          # T008
Task: "resolve_limits + EffectiveLimits in libs/shared/app_shared/limiter/limits.py"  # T009
```

## Parallel Example: User Story 2

```bash
# Author the US2 tests and the pure lock + item-field changes together:
Task: "Unit tests in tests/unit/test_match_lock.py"            # T015
Task: "locks.py fencing acquire/release"                        # T019
Task: "ScrapeResult carry-through fields in items.py"          # T020
Task: "Skip-clean integration test_spider_lock_collision.py"    # T017
Task: "Skip-clean integration test_target_unique_constraint.py" # T018
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (CRITICAL — blocks everything).
2. Phase 3 US1 → **STOP and VALIDATE**: run `tests/unit/test_rate_limiter.py`; cluster-wide rate + concurrency enforcement is the core protection and is independently shippable.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (P1) → distributed rate limiting + non-blocking backoff (MVP).
3. US2 (P1) → in-flight match dedup.
4. US3 (P2) → overflow / slot fairness under contention.
5. US4 (P3) → structured observability.
6. Polish → reactor-safety proof, lint, full skip-clean suite, quickstart walkthrough.

### Parallel Team Strategy

After Foundational: Developer A takes US1 (bucket/semaphore), Developer B takes US2 (match lock) — independent at the pure-logic layer; they coordinate the shared spider file edits (T013/T014 vs T022). US3 follows US1; US4 follows once US1–US3 spider paths exist.

---

## Notes

- `[P]` = different files, no dependency on an incomplete task.
- `[USn]` maps a task to its user story for traceability; Setup/Foundational/Polish carry no story tag.
- **No Docker daemon in this build env**: unit tests (pure Lua/limit/key/jitter/fencing logic) MUST actually run and pass; the 4 integration tests (`test_spider_lock_collision.py`, `test_target_unique_constraint.py`, `test_spider_overflow.py`, `test_observability_codes.py`) MUST `pytest.skip(...)` cleanly when Redis/Postgres/Scrapyd are absent — never fake success (SPEC-07..10 convention).
- **Reuse, do not recreate**: `RATE_LIMITED`/`LOCKED_ALREADY_RUNNING` codes, `unique(scrape_job_id, match_id)`, `DomainAccessRule`/`AccessPolicy` rate-config columns, `generic_price_spider` fetch path, `scrape_dispatch` Celery task, `redis_client` + access/budget Redis patterns.
- **Fail-closed** on Redis error (deliberately opposite of SPEC-10 budget's fail-open) — documented in both contracts; do not "harmonize" the two Redis usages.
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
