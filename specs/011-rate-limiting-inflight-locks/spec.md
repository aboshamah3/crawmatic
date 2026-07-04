# Feature Specification: Distributed Rate Limiting & In-Flight Locks

**Feature Branch**: `011-rate-limiting-inflight-locks`

**Created**: 2026-07-04

**Status**: Draft

**Input**: User description: "SPEC-11 — Distributed Rate Limiting & In-Flight Locks. Prevent domain blocking and duplicate work before large-scale real scraping."

## Clarifications

### Session 2026-07-04

All items resolved doc-first from `PROJECT_SPEC.md` and the existing codebase (no user prompts required).

- Q: Which limiter algorithm — token bucket or sliding window? → A: Atomic token bucket evaluated in a Lua script (doc §12 "e.g. a Lua token-bucket script"). Refill/window driven by the per-minute limit.
- Q: Where do per-domain rate and concurrency limit *values* come from — new config, or existing tables? → A: Reuse existing config. `DomainAccessRule` (SPEC-10) already carries `max_requests_per_minute`, `max_concurrent_requests`, `cooldown_seconds` and overrides `AccessPolicy`'s `max_requests_per_minute/hour/day`. SPEC-11 *reads* these; it adds **no** new rate-config columns. Domain rule overrides workspace/competitor policy defaults.
- Q: How is an overflowed (requeue-cap-exceeded) target represented? → A: A new `ScrapeTargetStatus.DEFERRED` member (enum renders as VARCHAR, so no DB enum migration). The spider marks the target `DEFERRED` and overflows it back to Celery `scrape_dispatch` for re-dispatch in a later batch. `PENDING`/`STARTED`/`SKIPPED`/`COMPLETED`/`FAILED` keep their existing meaning.
- Q: How does the system behave if Redis (the shared coordination store) is unreachable? → A: Fail safe — treat acquisition as not-permitted and back off / defer; never fetch uncontrolled (grounded in Constitution "Disciplined Scraping Runtime" NON-NEGOTIABLE). Captured as FR-023.
- Q: Are the structured error codes and the `unique(scrape_job_id, match_id)` constraint new? → A: No. `RATE_LIMITED` and `LOCKED_ALREADY_RUNNING` already exist in the error-code enum, and `unique(scrape_job_id, match_id)` already exists on `scrape_job_targets` (SPEC-08). SPEC-11 reuses/verifies them rather than creating them (FR-015 is a verification, not new schema).

## User Scenarios & Testing *(mandatory)*

The actors are the **platform operator** who triggers large scraping jobs, the **competitor domains** whose politeness limits must never be exceeded, and the **scraping runtime** (spiders running inside Scrapyd, orchestrated by Celery workers) that must coordinate across many concurrent processes through shared state.

### User Story 1 - Per-domain rate limits hold across every worker (Priority: P1)

The operator launches a large job that fans out many matches on the same competitor domain across multiple Scrapyd processes and Celery workers. Every outbound request first acquires permission from a shared, cross-process limiter so the *combined* request rate to that domain (per access method) stays within the configured limit — regardless of how many workers are running. When no permission is available, the request is rescheduled after a jittered delay rather than retried immediately.

**Why this priority**: This is the core protection the whole spec exists to deliver, and it is mandatory before any large real-scraping job. Without it, adding workers linearly multiplies the effective request rate and gets the platform blocked. It is independently valuable and testable on its own.

**Independent Test**: Drive many concurrent acquisition attempts (simulating N workers) against one domain key and confirm the number of granted permissions over a window never exceeds the configured rate/concurrency, and that denied attempts return a positive wait hint rather than being granted.

**Acceptance Scenarios**:

1. **Given** a per-domain rate limit of R requests/window for `DIRECT_HTTP` and many concurrent workers attempting to fetch that domain, **When** they all request permission, **Then** the total granted within the window does not exceed R and the rest are told to wait.
2. **Given** a per-domain concurrency (semaphore) limit of C for a domain+access-method, **When** more than C requests hold slots simultaneously, **Then** additional acquirers are denied until a slot is released or its TTL expires.
3. **Given** a request denied by the limiter, **When** it is rescheduled, **Then** the delay equals the limiter's wait hint plus a random jitter (bounded, e.g. 2–20s) so retries do not synchronize and hammer the domain.
4. **Given** a worker or spider process dies while holding a concurrency slot, **When** its slot's TTL expires, **Then** the slot is automatically reclaimed and the domain is not deadlocked.
5. **Given** two different workspaces scraping the same domain, **When** each acquires permission, **Then** their limits are enforced independently (keys are namespaced by workspace).

---

### User Story 2 - The same match is never scraped concurrently (Priority: P1)

A scheduled job and a manual job (or two overlapping batches) target the same competitor match at the same time. Exactly one of them proceeds; the other detects the in-flight lock and stands down cleanly, recording that the target was skipped because it was already running — instead of doing duplicate fetches and duplicate writes.

**Why this priority**: Duplicate concurrent scraping wastes the domain budget, produces duplicate observations, and risks race conditions in downstream price analysis. It is a core, independently testable guarantee.

**Independent Test**: Have two actors attempt to acquire the same match lock; confirm exactly one acquires it, the other is refused and marks its target `SKIPPED` with reason `LOCKED_ALREADY_RUNNING`, and that after the owner releases (or the TTL lapses) the lock is available again.

**Acceptance Scenarios**:

1. **Given** a match currently being scraped (lock held), **When** a second job targets the same match, **Then** the second target is skipped and marked `SKIPPED` / `LOCKED_ALREADY_RUNNING` (or requeued for later), and no second fetch occurs.
2. **Given** a spider that has acquired a match lock and finished persisting its observation, **When** it releases the lock, **Then** the lock is deleted only if the stored fencing token still matches the releaser's token.
3. **Given** a match lock that expired and was re-acquired by a new owner, **When** the previous (slow) owner attempts to release, **Then** the release is a no-op because the fencing token no longer matches — the new owner's lock is preserved.
4. **Given** an HTTP scrape vs. a browser scrape, **When** each acquires its match lock, **Then** the lock TTL reflects the mode (longer for browser) and comfortably exceeds the worst-case in-spider wait so a still-working owner never loses its lock mid-fetch.
5. **Given** two targets for the same match within one high-level job, **When** they are inserted, **Then** the `unique(scrape_job_id, match_id)` constraint prevents the duplicate at the job level before any lock is even attempted.

---

### User Story 3 - Rate-limited work overflows back to the queue instead of parking forever (Priority: P2)

Under heavy rate limiting, a request could otherwise sit inside a spider being rescheduled indefinitely, holding a scarce Scrapyd process slot and starving other work. Instead, in-spider rescheduling is capped (by retry count and by total wait time); once the cap is exceeded, the target is marked deferred and handed back to Celery `scrape_dispatch` to be picked up in a later batch, freeing the slot immediately.

**Why this priority**: It protects overall throughput and fairness under contention. The system still functions without it (US1/US2 are the correctness guarantees), but it prevents pathological slot starvation at scale, so it is important but secondary.

**Independent Test**: Force the limiter to keep denying a request; confirm the spider reschedules it only up to the configured cap (count and cumulative wait), then stops parking it, marks the target deferred, and emits an overflow signal back to Celery for re-dispatch — the process slot is released rather than held.

**Acceptance Scenarios**:

1. **Given** a request repeatedly denied by the limiter, **When** it has been rescheduled up to the requeue cap OR its cumulative in-spider wait reaches the cap, **Then** it is no longer parked in the spider.
2. **Given** a request that hit the requeue cap, **When** it overflows, **Then** its target is marked deferred and re-dispatched via Celery `scrape_dispatch` in a later batch (the match is not silently dropped).
3. **Given** an overflowed target, **When** it is re-dispatched later, **Then** it is treated as fresh work and again subject to the lock + limiter checks (no double-run).

---

### User Story 4 - Contention is observable via structured signals (Priority: P3)

When work is rate-limited or skipped due to an in-flight lock, the reason is recorded with a structured error code (`RATE_LIMITED`, `LOCKED_ALREADY_RUNNING`) on the affected target/attempt, so operators, the strategy optimizer, and access-policy tuning can see how often domains are throttling and how much duplicate work is being deduplicated.

**Why this priority**: Correctness does not depend on it, but the structured signals feed debugging, the future strategy optimizer, and client reporting. It is a thin layer over US1–US3.

**Independent Test**: Trigger a rate-limit denial and a lock collision; confirm each produces a persisted target/attempt outcome carrying the correct structured code, distinguishable from other error codes.

**Acceptance Scenarios**:

1. **Given** a request denied by the limiter that ultimately overflows, **When** its outcome is recorded, **Then** it carries the `RATE_LIMITED` code.
2. **Given** a target skipped for a held lock, **When** its outcome is recorded, **Then** it carries the `LOCKED_ALREADY_RUNNING` code and status `SKIPPED`.

---

### Edge Cases

- **Redis unavailable / transient error**: How does the limiter behave when the shared store cannot be reached? Must fail safe (deny/backoff rather than fetch uncontrolled) so an outage never removes domain protection.
- **Clock skew across workers**: Rate/window math must not depend on synchronized wall clocks across processes; it must be evaluated atomically in the shared store.
- **Lock TTL shorter than actual work**: If a fetch legitimately takes longer than the lock TTL, the fencing token guarantees the original owner cannot delete a newer owner's lock — but the design MUST size TTLs (backoff + requeue cap) so this is rare.
- **Process crash while holding a slot/lock**: TTL-based expiry must reclaim both semaphore slots and match locks; no permanent deadlock.
- **Jitter starvation**: With many contenders, jitter must be wide enough that they don't re-collide in lockstep, but bounded so a request isn't delayed unreasonably before hitting the overflow cap.
- **Overflow loop**: A target that keeps overflowing must not loop forever with no ceiling — re-dispatch must remain bounded by the job's own lifecycle / existing target status handling.
- **Non-blocking guarantee**: Acquisition must never block the Scrapy/Twisted reactor thread (no `time.sleep`, no synchronous blocking Redis call on the reactor).

## Requirements *(mandatory)*

### Functional Requirements

**Rate limiting (US1)**

- **FR-001**: The system MUST provide a distributed rate limiter, backed by shared state (Redis), that every outbound scrape request MUST consult and be granted by before fetching.
- **FR-002**: Rate-limit state MUST be keyed by workspace, domain, and access method using the keys `rate:{workspace_id}:{domain}:DIRECT_HTTP`, `rate:{workspace_id}:{domain}:PROXY_HTTP`, and `rate:{workspace_id}:{domain}:PLAYWRIGHT_PROXY`.
- **FR-003**: The system MUST enforce per-domain concurrency via a semaphore keyed `semaphore:{workspace_id}:{domain}:{access_method}`, denying acquirers beyond the configured concurrency limit.
- **FR-004**: Rate-limit and semaphore checks MUST be evaluated atomically in the shared store (e.g. a Lua token-bucket/sliding-window script) so concurrent workers cannot collectively exceed the limit due to races.
- **FR-005**: All limiter and semaphore keys MUST carry a TTL so that a crashed process never causes a permanent deadlock or a stuck slot.
- **FR-006**: When permission is denied, the limiter MUST return a wait hint, and the caller MUST reschedule the request after `wait_hint + bounded random jitter` (e.g. 2–20s) rather than retrying immediately.
- **FR-007**: Limiter acquisition MUST be non-blocking on the reactor: it MUST NOT call `time.sleep` or perform a synchronous blocking store call on the reactor thread (use async or `deferToThread`).
- **FR-008**: Limits (rate, window, concurrency, cooldown) MUST be read from existing DB configuration — `DomainAccessRule.max_requests_per_minute` / `max_concurrent_requests` / `cooldown_seconds` as the domain-level override, falling back to the resolved `AccessPolicy.max_requests_per_minute/hour/day`. SPEC-11 adds no new rate-config columns. When no rule/policy value is present, a safe built-in default applies.
- **FR-009**: Rate limiting MUST be enforced independently per workspace (no cross-workspace interference), honoring workspace isolation.

**In-flight match locking (US2)**

- **FR-010**: The system MUST provide an in-flight match lock keyed `lock:scrape:{workspace_id}:{match_id}` that prevents the same match from being scraped by more than one process at a time.
- **FR-011**: The spider MUST acquire the match lock immediately before fetching a match, and the spider (the same owner) MUST release it after persistence completes. The worker MUST NOT hold per-match locks across the dispatch boundary.
- **FR-012**: The lock value MUST be a unique fencing token; release MUST be a compare-and-delete (atomic) that deletes the key only if the stored token matches the releaser's token.
- **FR-013**: The match lock MUST carry a TTL sized to comfortably exceed the worst-case in-spider wait (rate-limit backoff + requeue cap), with a longer TTL for browser scrapes than HTTP scrapes (e.g. HTTP ~10 min, browser ~30 min as guidance).
- **FR-014**: When the lock is already held, the target MUST be skipped and marked `SKIPPED` with reason `LOCKED_ALREADY_RUNNING` (or requeued for a later attempt); no duplicate fetch may occur.
- **FR-015**: The `scrape_job_targets` table MUST enforce `unique(scrape_job_id, match_id)` so duplicate targets within a single high-level job are prevented at insert time. (This constraint already exists from SPEC-08; SPEC-11 verifies and relies on it rather than adding it.)
- **FR-016**: Downstream price-analysis MUST NOT depend on the scrape lock (it runs after release and is idempotent per variant).

**Requeue cap & overflow (US3)**

- **FR-017**: In-spider rescheduling MUST be bounded by both a maximum requeue count and a maximum cumulative in-spider wait per request.
- **FR-018**: When either cap is exceeded, the request MUST NOT continue to be parked in the spider; its target MUST be marked `DEFERRED` (a new `ScrapeTargetStatus` member) and handed back to Celery `scrape_dispatch` for re-dispatch in a later batch.
- **FR-019**: A re-dispatched (overflowed) target MUST re-enter the normal path and again be subject to lock + limiter checks, ensuring no double-run.

**Observability & error codes (US4)**

- **FR-020**: Rate-limit denials that result in overflow MUST be recorded against the target/attempt with the structured code `RATE_LIMITED`.
- **FR-021**: Lock-collision skips MUST be recorded with the structured code `LOCKED_ALREADY_RUNNING`.
- **FR-022**: Rate-limit hits MUST be observable (logged/counted) for operational visibility.

**Resilience**

- **FR-023**: If the shared store is unreachable, the limiter and match lock MUST fail **closed** (fail-safe) — treating the request as not-permitted and backing off/deferring — so that an outage never results in uncontrolled fetching against a domain. This is deliberately the *opposite* direction from the per-policy access-budget counters (SPEC-10), which fail open; the difference MUST be intentional and documented so the two Redis usages are not assumed to behave alike.

### Key Entities *(include if feature involves data)*

- **Rate-limit token bucket / window** (Redis): per `workspace_id + domain + access_method`; tracks available tokens/timestamps within a window; TTL-bounded.
- **Domain semaphore** (Redis): per `workspace_id + domain + access_method`; tracks currently-held concurrency slots; TTL-bounded to auto-reclaim on crash.
- **Match lock** (Redis): per `workspace_id + match_id`; holds a unique fencing token; TTL sized by scrape mode; owned and released by the spider.
- **scrape_job_target** (existing, extended usage): gains the ability to be marked `SKIPPED` (LOCKED_ALREADY_RUNNING) or deferred (overflow) and re-dispatched; enforces `unique(scrape_job_id, match_id)`.
- **Structured error codes** (existing enum): reuses `RATE_LIMITED` and `LOCKED_ALREADY_RUNNING`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With N concurrent workers targeting one domain, the observed combined request rate to that domain never exceeds the configured per-domain limit (0 breaches over a sustained test window).
- **SC-002**: When the same match is targeted by two overlapping jobs, exactly one fetch occurs and the other target is recorded as skipped — 0 duplicate observations for that match/job pair.
- **SC-003**: Under sustained rate limiting, no Scrapyd process slot is held by a single request beyond the configured cap; 100% of over-cap requests are overflowed back to the queue rather than parked.
- **SC-004**: A crashed worker never permanently blocks a domain or match: every semaphore slot and match lock is reclaimable within its TTL (0 permanent deadlocks).
- **SC-005**: The reactor is never blocked by limiter/lock acquisition (no `time.sleep` / synchronous blocking store call on the reactor thread), verified by inspection and test.
- **SC-006**: Every rate-limit overflow and lock collision is attributable to a structured code (`RATE_LIMITED` / `LOCKED_ALREADY_RUNNING`) on the target/attempt — 100% of such events carry a code.

## Assumptions

- Redis is available as the shared coordination store (already used for access budgets, status cache, and Celery broker in prior specs); a transient outage is handled fail-safe per FR-023.
- Access-method enum values (`DIRECT_HTTP`, `PROXY_HTTP`, `PLAYWRIGHT_PROXY`) and the structured error codes (`RATE_LIMITED`, `LOCKED_ALREADY_RUNNING`) already exist from prior specs (SPEC-07/08/10, §34) and are reused, not redefined.
- `scrape_jobs` / `scrape_job_targets` and the Celery `scrape_dispatch` orchestration exist (SPEC-08); this spec integrates with them (target status transitions, re-dispatch) rather than recreating them.
- The `generic_price_spider` and scrape-core fetch path exist (SPEC-07); this spec adds limiter acquisition and match-lock acquire/release into that path.
- Default numeric limits (rate, window size, concurrency, requeue cap, jitter range, lock TTLs) may start from the guidance values in the master doc (HTTP lock ~10 min, browser ~30 min, jitter 2–20s) and are configuration-driven so they can be tuned without code changes.
- The browser scraping service itself is delivered in a later spec (SPEC-14); this spec only sizes the browser lock TTL and reserves the `PLAYWRIGHT_PROXY` key so the browser path can reuse the same primitives.
- Per-domain limit values are keyed to the registrable domain used elsewhere in the platform (consistent with URL normalization / domain access rules from SPEC-05/10).
