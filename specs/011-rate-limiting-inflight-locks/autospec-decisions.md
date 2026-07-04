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
