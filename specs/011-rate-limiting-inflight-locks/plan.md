# Implementation Plan: Distributed Rate Limiting & In-Flight Locks

**Branch**: `011-rate-limiting-inflight-locks` (not on a git branch; feature dir is the anchor) | **Date**: 2026-07-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/011-rate-limiting-inflight-locks/spec.md`

## Summary

Make large real-scraping jobs safe by adding two cluster-wide, Redis-backed coordination
primitives to the existing SPEC-07/10 fetch path — **without any new DB table or rate-config
column**:

1. **Distributed domain rate limiting (US1).** An atomic **Lua token-bucket** keyed
   `rate:{workspace_id}:{domain}:{ACCESS_METHOD}` (refill driven by the per-minute limit)
   plus a TTL-bounded domain **concurrency semaphore** keyed
   `semaphore:{workspace_id}:{domain}:{access_method}`. Every outbound scrape request must be
   granted a token *and* a slot before it fetches; a denial returns a positive `wait_hint`.
   Limit **values are read from existing config** — `DomainAccessRule.max_requests_per_minute`
   / `max_concurrent_requests` / `cooldown_seconds` (domain override), falling back to the
   resolved `AccessPolicy.max_requests_per_minute/hour/day`, with a safe built-in default when
   neither is present. This layer sits *cluster-wide above* SPEC-10's per-policy `INCR`
   ceilings (`app_shared/access/budget.py`): SPEC-10 caps one policy's own usage and fails
   *open*; SPEC-11 caps the *combined* domain rate across every worker and fails **closed**.

2. **In-flight match locks (US2).** A spider-owned lock keyed
   `lock:scrape:{workspace_id}:{match_id}` whose value is a unique **fencing token**;
   acquired immediately before fetch, released after persistence via a Lua **compare-and-delete**.
   TTL is mode-sized (HTTP ~10 min, browser ~30 min) to comfortably exceed the worst-case
   in-spider wait. A held lock ⇒ the target is marked `SKIPPED` / `LOCKED_ALREADY_RUNNING`;
   no duplicate fetch occurs. Relies on (verifies, does not add) the existing
   `unique(scrape_job_id, match_id)` constraint from SPEC-08.

3. **Requeue cap & overflow (US3).** In-spider rescheduling is bounded by both a max requeue
   count and a max cumulative in-spider wait. On cap exceed, the target is marked **`DEFERRED`**
   (a new `ScrapeTargetStatus` member — enum renders VARCHAR, no DB enum migration) and handed
   back to Celery `scrape_dispatch` (via the existing `app_shared.messaging.enqueue` producer
   seam) for re-dispatch in a later batch, freeing the Scrapyd slot immediately.

4. **Observability (US4).** Overflowed rate-limit outcomes carry `RATE_LIMITED`; lock-collision
   skips carry `LOCKED_ALREADY_RUNNING` (both error codes already exist). Rate-limit hits,
   requeues, and dedup skips are logged/counted (Constitution §31).

**Reactor-safety decision (recorded in `scrape-core`, per Constitution V):** the Redis
token-bucket / semaphore / match-lock round-trips are **synchronous `redis` client calls
executed off-reactor via `deferToThread`** (the established `scrape_core.db.run_in_thread`
seam that SPEC-07/10 already use for every Redis/DB round-trip) — **not** a new async-redis
dependency. The *wait* between requeues is a **non-blocking reactor `callLater`-backed
Deferred delay** (`scrape_core.reactor.deferred_delay`), never `time.sleep` and never a
blocking Redis call on the reactor thread (FR-007, SC-005). This decision lives in
`scrape_core/limiter.py`'s module docstring.

The pure Redis Lua logic is DB/Scrapy/Twisted-free (mirrors `app_shared/access/budget.py`,
exhaustively unit-testable against a fake/real Redis); the reactor seam, spider integration,
overflow, and migration verification use skip-clean integration tests (SPEC-05..10 convention
— no live infra in this build environment).

## Technical Context

**Language/Version**: Python 3.13 (repo-wide `uv` workspace; `requires-python >=3.13,<3.14`).

**Primary Dependencies**: Redis (`redis` **sync** client — Lua `EVAL`/`register_script`,
already a locked dependency and the one consumed by `app_shared/access/budget.py`); Scrapy +
Twisted (spider integration + non-blocking `callLater` reschedule — extend only);
SQLAlchemy 2.x (read `DomainAccessRule`/`AccessPolicy`, transition target status — reuse);
Celery (overflow re-dispatch via the existing `app_shared.messaging.enqueue` producer). The
pure limiter/lock Redis logic depends on **stdlib only** (`secrets`, `time`, `dataclasses`)
plus the injected `redis.Redis`-shaped client. **No new third-party dependency.**

**Storage**: **No new PostgreSQL table, column, or migration.** Redis (`noeviction` instance —
same correctness-critical store as SPEC-10 budgets/ceilings) holds three TTL-bounded key
families: token bucket, domain semaphore, match lock. Existing Postgres is only *read*
(`DomainAccessRule`/`AccessPolicy` limit values via the already-cached SPEC-10 access
resolution) and *written* only through the existing `mark_target` target-status seam.

**Testing**: pytest. Pure Lua token-bucket / semaphore / fencing-lock logic → exhaustive unit
tests (refill math over a window, concurrent-grant bound, TTL presence, compare-and-delete
fencing, wait-hint sign). Reactor seam / spider integration / overflow / migration-verify →
integration tests that **skip cleanly** when Redis/Postgres/Scrapyd are absent (SPEC-01..10
precedent).

**Target Platform**: Linux multi-service deployment (`scrapyd-http-service`,
`scrapyd-browser-service`, `worker-service`, `api-service`, `redis`, `pgbouncer`, `postgres`).

**Project Type**: Backend monorepo (`uv` workspace) — `libs/shared` (`app_shared`),
`libs/scrape-core` (`scrape_core`), `apps/scrapers`, `apps/workers`, `apps/api`.

**Performance Goals**: 2,000 products & 10k–20k matches per workspace. Limiter/lock checks are
single-round-trip atomic Lua `EVAL`s (O(1), no scan). Limit values reuse the **already
Redis-cached** SPEC-10 effective-policy resolution (Principle IV — never a per-match DB walk).
Off-reactor via `deferToThread`, so acquisition never blocks the reactor (SC-005).

**Constraints**: Non-blocking reactor (no `time.sleep`, no sync Redis on the reactor thread —
FR-007/SC-005); atomic evaluation so concurrent workers cannot collectively exceed a limit
(FR-004, clock-skew-safe — all time math inside the Lua script on the Redis server clock);
TTL on every key so a crashed process never deadlocks (FR-005/SC-004); **fail-closed** on Redis
error (FR-023/SC-001 — deliberately the opposite of SPEC-10 budget's fail-open, documented in
both contracts); workspace-namespaced keys (FR-009, Principle II); `app_shared` MUST NOT import
Scrapy/Twisted/FastAPI or `apps/*`; `scrape_core` MAY import `app_shared`, never the reverse.

**Scale/Scope**: 1 new `app_shared/limiter/` package (pure Lua bucket + semaphore + fencing
lock + key builders + limit-value resolver) + 1 `scrape_core/limiter.py` reactor seam + 1
`scrape_core/reactor.py` non-blocking delay helper + spider integration (extend
`generic_price_spider` acquire/backoff/overflow + release seam in the persistence pipeline) +
1 new `ScrapeTargetStatus.DEFERRED` enum member + `mark_target`/dispatch-expansion `DEFERRED`
handling + Settings tuning knobs. **Out of scope (deferred):** actual Playwright browser
execution (SPEC-14 — here only the `PLAYWRIGHT_PROXY` key + 30-min browser-lock TTL are
reserved); "attach to existing job" dedup (§13 optional — v1 skips/requeues instead); learned
strategy optimizer (SPEC-12); any new DB table/column/migration (none — this spec is
Redis-and-enum only).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| **I. API-First, Service-Oriented** | PASS | Pure Redis limiter/lock logic lives in `app_shared/limiter/` (scraping-free, stdlib + injected redis client, no twisted/scrapy/fastapi). The reactor seam lives in `scrape-core` (imported by both Scrapy projects). The spider (`apps/scrapers`) imports `libs/*` only; overflow re-dispatch uses the existing `app_shared.messaging.enqueue` producer seam (task name constant `SCRAPE_DISPATCH_JOB`) — it never imports `apps/workers`. No new service, no new API endpoint. |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | PASS | Every Redis key is prefixed with `workspace_id` (`rate:{workspace_id}:…`, `semaphore:{workspace_id}:…`, `lock:scrape:{workspace_id}:{match_id}`) so two workspaces on the same domain never share a bucket/slot/lock (FR-009, US1 AS5). Limit values come from workspace-scoped `DomainAccessRule` / visible `AccessPolicy` via the existing scoped SPEC-10 resolution; target-status writes go through the workspace-scoped `mark_target`. No cross-workspace read/write introduced; cross-workspace key-namespacing test required. |
| **III. Variant-Level Pricing & Explicit Matching** | PASS (n/a) | No pricing/matching logic. The match lock keys on the existing `match_id`; price analysis is untouched and explicitly does **not** depend on the lock (FR-016, it runs after release, idempotent per variant). |
| **IV. Database-Driven Configuration** | PASS | Rate/concurrency/cooldown values are **read from the DB** (`DomainAccessRule` override → resolved `AccessPolicy` fallback → safe built-in default) — no hardcoded per-domain limits and **no new config column** (FR-008). Resolution reuses SPEC-10's already-batched, Redis-cached effective-policy result (never an N+1 per-match walk). All numeric knobs (jitter range, requeue cap, lock TTLs, default limit) are env-tunable `Settings`. |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | PASS | **The core of this spec.** Every limiter/lock Redis round-trip runs **off-reactor via `deferToThread`** (`run_in_thread`) — the decision is recorded in `scrape_core/limiter.py` (Constitution V requires it be decided in scrape-core). The inter-requeue wait is a **non-blocking `callLater`-backed Deferred** — no `time.sleep`, no sync Redis on the reactor (FR-007, SC-005). The spider still only persists; lock **release happens in the existing off-reactor batched persistence pipeline** after the write (FR-011/FR-016), never adding a reactor hop. Idempotent dispatch preserved: overflow re-dispatch re-enters the same lock+limiter gate (FR-019). |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | PASS | Adds coordination *before* the fetch; introduces no new access method and no external service. Keys cover the internal `AccessMethod`s: `DIRECT_HTTP`, `PROXY_HTTP`, and `PLAYWRIGHT_PROXY` (the latter two reserved for later specs); `DIRECT_HTTP_RETRY` reuses the `DIRECT_HTTP` bucket/semaphore (a retry counts against the same direct budget — FR-002), so no separate key is needed for it. SSRF and the existing fetch guard are untouched. On Redis outage the limiter **denies** (never fetches uncontrolled — FR-023), strictly *tightening* access. No raw HTML/screenshot storage introduced. |
| **VII. Monetary & Extraction Correctness** | PASS (n/a) | No money or extraction logic. Token counts and TTLs are integers/seconds, never currency. |
| **VIII. Scale-Safe Data & Concurrency** | PASS | **This spec delivers the §12/§13 pillars of Principle VIII directly**: distributed per-workspace+domain+access-method rate limiting + concurrency semaphores (cluster-wide, not per-worker) and fencing-token in-flight dedup. No hot-row contention added — lock/limiter live in Redis, not a DB row; target status uses the existing single-writer `mark_target` + aggregated counters (no per-target increment). No new append table (nothing to partition). Overflow frees Scrapyd slots under contention (SC-003). |

**Gate result: PASS** — no violations; Complexity Tracking table left empty. The intentional
**fail-closed** Redis behavior (opposite of SPEC-10 budget's fail-open) is not a deviation —
it is mandated by FR-023 and the Constitution's "never fetch uncontrolled" rule, and is
documented in both contracts.

## Project Structure

### Documentation (this feature)

```text
specs/011-rate-limiting-inflight-locks/
├── plan.md              # This file
├── research.md          # Phase 0 output — decisions & rationale
├── data-model.md        # Phase 1 output — Redis key entities, enum delta, Settings, reused schema
├── quickstart.md        # Phase 1 output — validation scenarios
├── contracts/           # Phase 1 output
│   ├── rate-limiter.md          # Lua token-bucket + semaphore acquire/release + limit resolution
│   ├── match-lock.md            # fencing-token acquire + Lua compare-and-delete release
│   ├── reactor-seam.md          # deferToThread decision + non-blocking callLater delay
│   ├── spider-integration.md    # generic_price_spider acquire/backoff/requeue-cap + pipeline release
│   ├── overflow-dispatch.md     # DEFERRED enum, mark_target, enqueue re-dispatch, expansion include
│   └── observability.md         # Settings knobs, error-code mapping, logs/metrics
├── spec.md
└── tasks.md             # /speckit-tasks output (NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py                         # +ScrapeTargetStatus.DEFERRED (VARCHAR — no DB enum migration)
│                                    #  (AccessMethod, ScrapeErrorCode.RATE_LIMITED /
│                                    #   LOCKED_ALREADY_RUNNING already exist — reuse)
├── limiter/                         # NEW package — PURE Redis logic (redis-client param; no twisted/scrapy)
│   ├── __init__.py
│   ├── keys.py                      #   key builders: rate:/semaphore:/lock:scrape: (workspace-namespaced)
│   ├── bucket.py                    #   Lua token-bucket acquire (returns granted + wait_hint) + semaphore
│   │                               #     acquire/release; TTL on every key; fail-CLOSED on redis error
│   ├── locks.py                     #   fencing-token acquire (SET NX PX) + Lua compare-and-delete release
│   └── limits.py                    #   resolve effective (rate, window, concurrency, cooldown, lock_ttl)
│                                    #     from DomainAccessRule override → AccessPolicy → Settings default
├── config.py                        # +RATE_LIMIT_* / MATCH_LOCK_* / REQUEUE_* / JITTER_* tuning knobs
├── jobs/targets.py                  # mark_target: allow DEFERRED terminal-ish transition (overflow)
└── messaging.py                     # (unchanged) reused enqueue(SCRAPE_DISPATCH_JOB) overflow seam

libs/scrape-core/scrape_core/
├── reactor.py                       # NEW — deferred_delay(seconds): non-blocking callLater-backed Deferred
└── limiter.py                       # NEW — reactor seam: acquire_permission()/acquire_lock()/release_lock()
                                     #   wrap the app_shared.limiter pure funcs via run_in_thread
                                     #   (deferToThread). *Records the async-vs-deferToThread decision.*

apps/scrapers/price_monitor/
└── spiders/generic_price_spider.py  # EXTEND — before each dispatch (start/errback): acquire domain token
                                     #   + semaphore slot + match lock (off-reactor). Denied ⇒ non-blocking
                                     #   deferred_delay(wait_hint + jitter) then re-try, bounded by requeue
                                     #   count + cumulative wait; on cap ⇒ mark DEFERRED + enqueue overflow.
                                     #   Lock held ⇒ SKIPPED/LOCKED_ALREADY_RUNNING. Thread fencing token
                                     #   through to the pipeline; release semaphore slot on response.

libs/scrape-core/scrape_core/pipelines.py
                                     # EXTEND — after the batched persistence write, release each item's
                                     #   match lock (Lua compare-and-delete with its fencing token) in the
                                     #   SAME off-reactor flush; RATE_LIMITED/LOCKED_ALREADY_RUNNING codes
                                     #   flow through the existing ScrapeResult → mark_target path.

apps/workers/app/workers/tasks_jobs.py
                                     # EXTEND — dispatch-expansion target query includes DEFERRED alongside
                                     #   PENDING so an overflowed target is re-dispatched (FR-018/FR-019);
                                     #   DEFERRED → STARTED on re-pickup, re-subject to lock+limiter.

tests/  (per-package, mirroring SPEC-10 layout)
├── unit/         — Lua token-bucket refill/bound math, semaphore cap, fencing compare-and-delete,
│                   wait-hint sign, limit-value resolution precedence, key formatting, jitter bounds
└── integration/  — skip-clean: Redis limiter under N concurrent acquirers (SC-001), TTL reclaim
                    (SC-004), match-lock collision → SKIPPED (SC-002), overflow → DEFERRED + enqueue
                    (SC-003), Redis-down fail-closed (FR-023), unique(scrape_job_id,match_id) verify
```

**Structure Decision**: Reuse the established monorepo layout exactly, mirroring the SPEC-10
split that already proved itself in this repo. The **pure Redis logic** goes in a new
`app_shared/limiter/` package — the direct sibling of `app_shared/access/budget.py` (redis-
client parameter, stdlib otherwise, no Scrapy/Twisted, exhaustively unit-testable). The
**reactor-safe orchestration** (the `deferToThread` decision + non-blocking `callLater` delay)
lives in `scrape-core` (`scrape_core/limiter.py` + `scrape_core/reactor.py`) because
Constitution V requires this decision to be owned there and it is the only member allowed to
touch Twisted; both Scrapy projects import it. The spider is extended at its existing
`start()`/`errback()` dispatch seams and the persistence pipeline's existing off-reactor
`_flush_batch` (lock release) — no new spider and no new reactor hop. Overflow reuses the
existing `app_shared.messaging.enqueue` → `SCRAPE_DISPATCH_JOB` producer seam. The only schema
change anywhere is one new **VARCHAR** enum member (`ScrapeTargetStatus.DEFERRED`) — **no
Alembic migration** (enums render as VARCHAR, confirmed by SPEC-08/10 precedent).

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
