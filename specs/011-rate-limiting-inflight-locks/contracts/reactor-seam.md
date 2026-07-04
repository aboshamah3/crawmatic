# Contract: Reactor-Safe Seam (the async-vs-deferToThread decision)

**Modules**: `libs/scrape-core/scrape_core/limiter.py` and `libs/scrape-core/scrape_core/reactor.py`.
This is the **only** place allowed to touch Twisted for this feature (Constitution V requires
the reactor-safety decision to be owned in `scrape-core`). Covers FR-007; SC-005.

---

## DECISION OF RECORD (state verbatim in `scrape_core/limiter.py` docstring)

> The distributed rate-limiter, semaphore, and match-lock Redis round-trips are **synchronous
> `redis` client `EVAL`/`SET`/`ZADD` calls executed off the Twisted reactor via
> `deferToThread`** (the existing `scrape_core.db.run_in_thread` seam SPEC-07/10 use for every
> Redis/DB round-trip). **No async-redis client is introduced.** The wait between requeues is a
> **non-blocking reactor `callLater`-backed `Deferred`** (`scrape_core.reactor.deferred_delay`).
> There is **no** `time.sleep` and **no** synchronous Redis call on the reactor thread anywhere
> in the scrape path (FR-007, SC-005). Rationale in `research.md` D2.

## `scrape_core/reactor.py`

```python
def deferred_delay(seconds: float) -> Deferred:
    """Return a Deferred that fires after `seconds` via reactor.callLater — the reactor keeps
    servicing other requests while this one waits. Never blocks a thread, never time.sleep."""
```
- Implementation: `d = Deferred(); reactor.callLater(seconds, d.callback, None); return d`.
- Awaited from the spider's `async def start()` / `errback()` coroutines
  (`await deferred_delay(...)`) — the project runs `AsyncioSelectorReactor`, so awaiting a
  Deferred is native (SPEC-10 precedent).

## `scrape_core/limiter.py` — reactor wrappers over the pure `app_shared.limiter` funcs

```python
async def acquire_permission(redis, *, workspace_id, domain, access_method,
                             limits, settings, sem_token) -> Permission: ...
async def release_slot(redis, *, key, token) -> None: ...
async def acquire_lock(redis, *, workspace_id, match_id, mode, settings) -> LockGrant | None: ...
async def release_lock(redis, *, key, token) -> None: ...
```
- Each `await run_in_thread(<pure app_shared.limiter fn>, ...)` — off-reactor (`deferToThread`).
- `acquire_permission` returns a `Permission` carrying `granted`, `wait_hint_seconds`, the
  semaphore `key`+`token` (for later release), or a denial. It combines token-bucket **then**
  semaphore (both must grant); if the bucket denies, the semaphore is not touched; if the
  semaphore denies after a token was taken, the token is *not* refunded (acceptable — the bucket
  self-refills; simpler and never over-grants).
- `acquire_lock` builds the mode-sized TTL, generates the fencing token, calls
  `acquire_match_lock`, and returns the `key`+`token` on success or `None` when already held.
- All wrappers propagate the pure layer's **fail-closed** semantics (a Redis error surfaces as
  not-granted / `None`, never as an exception that could be mistaken for "proceed").

## Non-negotiable checks (SC-005, verified by inspection + test)
- `grep` the scrape path ⇒ **zero** `time.sleep` and **zero** synchronous `redis`/`EVAL` call
  outside a `run_in_thread`/`deferToThread` boundary.
- The pipeline lock-release runs inside the **existing** off-reactor `_flush_batch`
  (`run_in_thread`) — no new reactor hop is added (Principle V).
