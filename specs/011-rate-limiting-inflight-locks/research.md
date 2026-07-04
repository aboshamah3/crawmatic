# Phase 0 Research: Distributed Rate Limiting & In-Flight Locks

All Technical Context unknowns were resolved doc-first from `PROJECT_SPEC.md` (§12, §13, §26,
§34), the Constitution (Principles V, VIII), the spec's Clarifications section, and the
existing codebase (SPEC-07/08/10). No open `NEEDS CLARIFICATION` remains.

---

## D1 — Limiter algorithm: atomic Lua token bucket

- **Decision**: A per-key **token bucket** evaluated entirely inside a Redis **Lua** script.
  State per key: `{tokens, last_refill_ts}` in a Redis hash. Refill rate = `max_requests_per_minute / 60` tokens/sec; capacity = `max_requests_per_minute` (a full minute's burst).
  On acquire the script computes elapsed time on the **Redis server clock**, refills
  `min(capacity, tokens + elapsed*rate)`, and if `tokens >= 1` decrements and grants; else
  denies and returns `wait_hint = ceil((1 - tokens) / rate)` seconds. Every touch re-`PEXPIRE`s
  the key to a bounded TTL (`window + slack`).
- **Rationale**: PROJECT_SPEC §12 names "a Lua token-bucket script" explicitly; the spec's
  clarify session fixed token-bucket. Doing all arithmetic inside one `EVAL` makes the
  check **atomic** across all workers (FR-004) and **clock-skew-immune** (FR-004 edge case) —
  no worker's wall clock participates. A single hash + PEXPIRE is O(1) with no key scan
  (Principle VIII).
- **Alternatives considered**: (a) *Sliding-window log* (sorted set of timestamps) — more
  memory, ZREMRANGEBYSCORE per call, and the spec/doc chose bucket. (b) *Reuse SPEC-10's
  `INCR`+`EXPIRE` fixed windows* — those are per-*policy* and fail *open*; they cannot express
  a smooth cross-worker domain rate and are the wrong failure mode for FR-023. SPEC-11 layers
  *above* them, it does not replace them.

## D2 — Reactor safety: `deferToThread` (sync redis off-reactor), NOT async-redis

- **Decision**: The token-bucket / semaphore / lock Redis calls are **synchronous `redis`
  client `EVAL`s executed off the reactor via `deferToThread`** — the exact
  `scrape_core.db.run_in_thread` seam SPEC-07/10 already use for every Redis/DB round-trip in
  the spider. **No** `redis.asyncio` / async client is introduced. The **wait between requeues**
  is a **non-blocking reactor `callLater`-backed `Deferred`** (`scrape_core.reactor.deferred_delay`),
  awaited from the `async def start()`/`errback()` coroutines. The decision is recorded in
  `scrape_core/limiter.py`'s docstring (Constitution V requires it be owned in scrape-core).
- **Rationale**: Consistency beats novelty — the spider already `await run_in_thread(...)`s its
  SPEC-10 budget/ceiling checks; the same seam carries the SPEC-11 checks with zero new
  dependency, one connection pool, and identical fork-safety. `time.sleep` and sync Redis on
  the reactor are both banned (FR-007, SC-005, Principle V); `callLater` gives a non-blocking
  delay that yields the reactor to other requests while one request backs off.
- **Alternatives considered**: (a) *`redis.asyncio` on the reactor* — a second client stack,
  new lock-free connection semantics, and no measured need; rejected for footprint. (b)
  *`time.sleep` in a thread* — would pin a thread-pool thread for the whole backoff, starving
  other off-reactor work under contention; `callLater` costs nothing while waiting.

## D3 — Fail-closed on Redis error (opposite of SPEC-10 budget)

- **Decision**: Any Redis error (unreachable, timeout, script error) during **acquire** ⇒
  treat as **not granted** — deny the token, deny the semaphore slot, and for the match lock
  treat "cannot confirm we own it" as *do not fetch*. The caller then backs off / defers.
- **Rationale**: FR-023 + Constitution "never fetch uncontrolled". A rate limiter that opens on
  outage removes exactly the protection it exists to give. This is deliberately the **inverse**
  of `app_shared/access/budget.py`, which fails *open* (a proxy-budget counter outage must not
  wedge scraping, and the cluster limiter — this spec — owns strict enforcement). Both contracts
  state the divergence explicitly so no future reader "harmonizes" them by mistake.
- **Note**: On **release**, a Redis error is logged and swallowed (the key's TTL reclaims it) —
  release failure must never crash persistence or block the batch.

## D4 — Limit values: read existing config, add no columns

- **Decision**: Effective limits resolve, per `(workspace, domain, access_method)`, as:
  `DomainAccessRule.max_requests_per_minute` / `max_concurrent_requests` / `cooldown_seconds`
  (domain-level override, if an enabled rule matches) **→ else** the resolved
  `AccessPolicy.max_requests_per_minute` (the token-bucket refill; `.../_hour`/`_day` remain
  SPEC-10's own ceilings) **→ else** a safe built-in `Settings` default
  (`RATE_LIMIT_DEFAULT_PER_MINUTE`, `RATE_LIMIT_DEFAULT_CONCURRENCY`). This reuses the
  **already-resolved, Redis-cached** SPEC-10 effective policy + domain rule the spider's
  `load_targets` bounded-load already fetches — **no extra query, no new column** (FR-008,
  Clarify Q2).
- **Rationale**: The clarify session fixed "reuse existing config, add no rate-config columns."
  `DomainAccessRule` (SPEC-10) already carries all three override values as NOT-NULL columns;
  `AccessPolicy` carries the per-minute/hour/day fallback. The spider already loads both in one
  bounded pass and caches them — SPEC-11 just *reads* the resolved object.
- **Alternatives considered**: A new `rate_limits` table / new columns — explicitly rejected by
  clarify and by Principle IV's "already batch-resolved and cached" rule.

## D5 — Domain concurrency semaphore

- **Decision**: A TTL-bounded counting semaphore per `semaphore:{workspace_id}:{domain}:{access_method}`.
  Implemented as a Redis **sorted set** of `{fencing_token → expiry_ts}`: acquire = Lua that
  first `ZREMRANGEBYSCORE`-purges expired members (auto-reclaim of crashed holders, FR-005/
  SC-004), then if `ZCARD < max_concurrent_requests` adds the caller's token and grants, else
  denies. Release = `ZREM` the token. The whole key `PEXPIRE`s to slot-TTL + slack.
- **Rationale**: A plain `INCR`/`DECR` counter cannot self-heal a crashed holder without a
  separate reaper; the sorted-set-with-expiry pattern reclaims slots purely by TTL inside the
  acquire path (no background job), satisfying "a dead worker never deadlocks the domain"
  (US1 AS4, SC-004). The slot is held only for the *fetch* (acquired at dispatch, released when
  the response/failure returns), distinct from the match lock which spans fetch→persist.
- **Alternatives considered**: `SET`-based lock-per-slot (N keys) — more keys, awkward "which
  slot is free" scan; sorted-set is one key, O(log n) acquire.

## D6 — Match lock: fencing token + Lua compare-and-delete

- **Decision**: `lock:scrape:{workspace_id}:{match_id}`, value = a 128-bit unique token
  (`secrets.token_hex`). Acquire = `SET key token NX PX ttl` (atomic; failure ⇒ held ⇒ skip).
  Release = Lua `if redis.call('GET',k)==token then return redis.call('DEL',k) else return 0`
  (compare-and-delete). TTL is mode-sized: HTTP `MATCH_LOCK_HTTP_TTL_SECONDS ≈ 600`, browser
  `MATCH_LOCK_BROWSER_TTL_SECONDS ≈ 1800` (§13), each ≫ the worst-case in-spider wait
  (requeue-cap × max backoff) so a still-working owner never loses its lock (US2 AS4).
- **Rationale**: Directly the §13 / Principle VIII / spec FR-010–FR-013 design. The fencing
  token guarantees an expired-then-reacquired lock is never deleted by the slow prior owner
  (US2 AS3). `SET NX PX` is the canonical single-round-trip acquire; the compare-and-delete Lua
  is the canonical safe release.
- **Ownership/lifecycle**: the **spider** acquires immediately before fetch and the **spider's
  persistence pipeline** releases after the write (FR-011) — the token is threaded from the
  acquiring code to the pipeline via the request `meta` → `ScrapeResult`. The worker never
  holds a per-match lock across the dispatch boundary (§13). `price_analysis` runs after release
  and needs no lock (FR-016).

## D7 — Requeue cap, jitter, and overflow to Celery

- **Decision**: Bound in-spider rescheduling by **both** `REQUEUE_MAX_ATTEMPTS` (count) and
  `REQUEUE_MAX_TOTAL_WAIT_SECONDS` (cumulative). Each denial reschedules after
  `wait_hint + random.uniform(JITTER_MIN_SECONDS=2, JITTER_MAX_SECONDS=20)` via the
  non-blocking `deferred_delay` (FR-006). When **either** cap is exceeded, stop parking:
  mark the target **`DEFERRED`** (new enum member) via `mark_target` off-reactor, and
  `app_shared.messaging.enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"scrape_job_id": …})`
  so a later batch re-dispatches it (FR-017/FR-018). The overflowed target re-enters the normal
  path and is re-subject to lock+limiter (FR-019).
- **Rationale**: §12 "Requeue cap and overflow" verbatim: unbounded in-spider requeue holds a
  Scrapyd slot and starves other work (SC-003). Wide bounded jitter de-synchronizes contenders
  (edge case "jitter starvation") without unbounded delay. The `enqueue` producer seam already
  exists (SPEC-08) so the spider overflows **without importing `apps/workers`** (Principle I).
- **Expansion change**: `dispatch_job`'s target-expansion query today selects `status == PENDING`;
  it must also include `DEFERRED` so an overflowed target is actually re-picked (and reset
  `DEFERRED → STARTED` on pickup). Overflow re-dispatch stays bounded by the job's own lifecycle
  (a finalized job is skipped — edge case "overflow loop").

## D8 — Enum & error codes: reuse, one VARCHAR add, no migration

- **Decision**: Add `ScrapeTargetStatus.DEFERRED = "DEFERRED"`. **Reuse** the existing
  `ScrapeErrorCode.RATE_LIMITED` (overflow outcome, FR-020) and `LOCKED_ALREADY_RUNNING`
  (lock-collision SKIPPED, FR-021). **Reuse/verify** the existing
  `unique(scrape_job_id, match_id)` on `scrape_job_targets` (SPEC-08) — FR-015 is a
  verification, not new schema.
- **Rationale**: The clarify session confirmed all three already exist except `DEFERRED`. The
  `status` column is a VARCHAR-rendered `StrEnum` (`enum_column`), so adding a member needs
  **no Alembic migration** (SPEC-08/10 precedent: enum members render as strings). No DB enum
  type exists to `ALTER`.
- **`mark_target` change**: `DEFERRED` is a non-terminal, re-dispatchable status. `mark_target`
  must accept a transition to `DEFERRED` (stamp no `completed_at`; it is not terminal) and the
  overflow path calls it. The job-finalization `_TERMINAL_TARGET_STATUSES` set is **unchanged**
  (DEFERRED is not terminal — a job with a DEFERRED target is not "all terminal", so it stays
  open until the re-dispatch resolves it — edge case "overflow loop" bounded by job lifecycle).

## D9 — Testing strategy under no-live-infra

- **Decision**: Pure Lua/limit-resolution logic → exhaustive **unit** tests (a real
  ephemeral Redis if available, else `fakeredis`-style/skip-clean) covering: refill math over
  a window, "≤ R grants across N concurrent acquirers", semaphore cap + expiry reclaim,
  fencing compare-and-delete (own vs stolen), wait-hint > 0 on denial, limit precedence
  (rule→policy→default), key formatting, jitter bounds. Reactor seam / spider / overflow /
  migration-verify → **skip-clean integration** tests (SPEC-01..10 convention — no Docker
  daemon in this env; live Redis/Postgres/Scrapyd checks SKIP rather than fail).
- **Rationale**: Matches the ratified project convention (MEMORY: no Docker daemon in build
  env — author tests that SKIP cleanly, never fake success). The correctness core (the Lua
  scripts) is deterministic and unit-testable without the reactor.

---

### Resolved unknowns summary

| Unknown | Resolution |
|---|---|
| Algorithm | Atomic Lua token bucket (D1) |
| Reactor safety (async vs deferToThread) | `deferToThread` for Redis; `callLater` Deferred for backoff (D2) |
| Redis-outage behavior | Fail-closed on acquire; swallow on release (D3) |
| Where limits come from | Existing `DomainAccessRule`→`AccessPolicy`→`Settings` default; no new column (D4) |
| Concurrency mechanism | Sorted-set semaphore with in-acquire expiry purge (D5) |
| Lock safety | `SET NX PX` + Lua compare-and-delete fencing token, mode-sized TTL (D6) |
| Overflow | Requeue cap (count+wait) → `DEFERRED` → `enqueue(SCRAPE_DISPATCH_JOB)` (D7) |
| Schema impact | +1 VARCHAR enum member, no migration; reuse error codes + unique constraint (D8) |
| Testing | Unit (pure Lua) + skip-clean integration (D9) |
