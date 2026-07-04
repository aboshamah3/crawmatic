# Autospec Decisions — SPEC-11 Distributed Rate Limiting & In-Flight Locks

All questions answered doc-first from `/srv/crawmatic/PROJECT_SPEC.md`. No user prompts were required.

## specify

- [specify] Q: What are the rate-limit / semaphore / lock key formats? → A: `rate:{workspace_id}:{domain}:{ACCESS_METHOD}`, `semaphore:{workspace_id}:{domain}:{access_method}`, `lock:scrape:{workspace_id}:{match_id}` (source: doc §12, §13)
- [specify] Q: Who owns the match lock lifecycle? → A: Spider acquires immediately before fetch and releases after persistence; worker does not hold per-match locks across dispatch; fencing-token compare-and-delete release (source: doc §13 "Lock lifecycle (single owner)")
- [specify] Q: Lock TTL values? → A: HTTP ~10 min, browser ~30 min, sized to exceed worst-case in-spider wait (source: doc §13)
- [specify] Q: Delay/jitter formula on rate-limit denial? → A: `wait_time + random(2,20s)`, non-blocking reschedule (source: doc §12)
- [specify] Q: Behavior when in-spider requeue cap exceeded? → A: Mark target deferred, overflow back to Celery `scrape_dispatch` for later batch (source: doc §12 "Requeue cap and overflow")
- [specify] Q: Which structured error codes apply? → A: `RATE_LIMITED`, `LOCKED_ALREADY_RUNNING` (source: doc §34, §35 spec-11)
- [specify] Q: Non-blocking requirement? → A: Limiter must be non-blocking on the reactor — async/deferToThread Redis (Lua token bucket for atomicity); never time.sleep (source: doc §12)
- [specify] Q: Redis-unavailable behavior (doc-silent)? → A: Fail safe = deny/backoff rather than fetch uncontrolled (default: derived from Constitution "Disciplined Scraping Runtime" NON-NEGOTIABLE + intent of §12 "mandatory before large jobs"; flagged as FR-023 assumption)

## clarify

No user prompts required — every material ambiguity resolved doc-first / codebase-first.

- [clarify] Q: Limiter algorithm token bucket vs sliding window? → A: Atomic Lua token bucket (source: doc §12 "e.g. a Lua token-bucket script")
- [clarify] Q: Source of per-domain rate/concurrency limit values? → A: Reuse existing `DomainAccessRule.max_requests_per_minute`/`max_concurrent_requests`/`cooldown_seconds` overriding `AccessPolicy.max_requests_per_*`; no new columns (source: codebase libs/shared/app_shared/models/access.py + doc SPEC-10 "Domain access rule overrides competitor/workspace defaults")
- [clarify] Q: Representation of an overflowed target? → A: New `ScrapeTargetStatus.DEFERRED` member (VARCHAR-rendered enum, no DB migration); re-dispatched via Celery `scrape_dispatch` (source: doc §12 "mark the target as deferred and overflow it back to Celery scrape_dispatch"; codebase enums.py shows current members PENDING/STARTED/COMPLETED/FAILED/SKIPPED)
- [clarify] Q: Redis-unreachable behavior? → A: Fail safe deny/backoff (default: Constitution "Disciplined Scraping Runtime"; FR-023)
- [clarify] Q: Are RATE_LIMITED/LOCKED_ALREADY_RUNNING and unique(scrape_job_id,match_id) new? → A: No — already exist (RATE_LIMITED/LOCKED_ALREADY_RUNNING in enums.py; unique constraint on scrape_job_targets from SPEC-08). SPEC-11 reuses/verifies (source: codebase enums.py:260-261, jobs.py:126-129)
