# Contract: Distributed Rate Limiter (token bucket + semaphore)

**Module**: `libs/shared/app_shared/limiter/` — `bucket.py`, `keys.py`, `limits.py`
(pure Redis logic; takes a `redis.Redis`-shaped client; **no** Scrapy/Twisted/FastAPI import —
mirrors `app_shared/access/budget.py`). Reactor wrapping is in `contracts/reactor-seam.md`.

Covers FR-001..FR-009, FR-023; US1; SC-001, SC-004, SC-005.

---

## Keys (`keys.py`)

```
rate_key(workspace_id, domain, access_method)      -> "rate:{workspace_id}:{domain}:{access_method}"
semaphore_key(workspace_id, domain, access_method) -> "semaphore:{workspace_id}:{domain}:{access_method}"
```
- `access_method` is the `AccessMethod` **value** string (`DIRECT_HTTP` / `PROXY_HTTP` /
  `PLAYWRIGHT_PROXY`) (FR-002/FR-003).
- `domain` is the registrable domain already loaded on `Competitor.domain` (never a URL guess;
  consistent with SPEC-05/10 — spec Assumptions).
- `workspace_id` MUST be first after the family prefix (FR-009, Principle II).

## Effective limits (`limits.py`)

```python
@dataclass(frozen=True)
class EffectiveLimits:
    per_minute: int        # token-bucket capacity + refill basis
    concurrency: int       # semaphore max
    cooldown_seconds: int  # per-domain cooldown (reuses SPEC-10 check_domain_cooldown)

def resolve_limits(*, domain_rule, access_policy, settings) -> EffectiveLimits: ...
```
- Precedence (D4, FR-008): enabled matching `DomainAccessRule` override →
  resolved `AccessPolicy.max_requests_per_minute` → `Settings.RATE_LIMIT_DEFAULT_PER_MINUTE`
  / `RATE_LIMIT_DEFAULT_CONCURRENCY`. **Reads the already-resolved, Redis-cached SPEC-10 objects
  the spider's `load_targets` already holds** — no new query, no new column.
- `per_minute`/`concurrency` are always ≥ 1 after resolution (a safe floor).

## Token bucket (`bucket.py`)

```python
@dataclass(frozen=True)
class AcquireResult:
    granted: bool
    wait_hint_seconds: int   # > 0 when not granted (FR-006); 0 when granted

def acquire_token(redis, *, key: str, capacity: int, ttl_seconds: int) -> AcquireResult: ...
```
- **Atomic Lua** `EVAL` (registered once via `register_script`). Script logic:
  1. read hash `{tokens, ts}` (default `tokens=capacity`, `ts=now`);
  2. `refill = (now - ts) * capacity / 60`; `tokens = min(capacity, tokens + refill)`; `ts = now`;
  3. if `tokens >= 1`: `tokens -= 1`; write back; `PEXPIRE key ttl`; return `{1, 0}`;
     else: write back; `PEXPIRE key ttl`; return `{0, ceil((1 - tokens) * 60 / capacity)}`.
- `now` is the **Redis server time** (`redis.call('TIME')`) — no worker wall clock (FR-004).
- Every path `PEXPIRE`s the key (FR-005). `ttl_seconds = 2*60 + Settings.RATE_LIMIT_KEY_TTL_SLACK_SECONDS`.
- **Redis error ⇒ `AcquireResult(granted=False, wait_hint_seconds=default_backoff)` (fail-closed, FR-023).**

## Semaphore (`bucket.py`)

```python
def acquire_slot(redis, *, key: str, limit: int, token: str,
                 slot_ttl_seconds: int, key_ttl_seconds: int) -> bool: ...   # granted?
def release_slot(redis, *, key: str, token: str) -> None: ...
```
- **acquire** — atomic Lua: `ZREMRANGEBYSCORE key -inf now` (purge expired holders, SC-004);
  if `ZCARD key < limit` → `ZADD key (now+slot_ttl) token`, `PEXPIRE key key_ttl`, return `1`;
  else return `0`.
- **release** — `ZREM key token` (idempotent; a missing member is a no-op). Redis error on
  release is logged + swallowed (TTL reclaims — D3).
- **acquire Redis error ⇒ return `False` (fail-closed, FR-023).**

## Invariants (unit-tested — SC-001/SC-004)
- Across N concurrent `acquire_token` calls in one window, granted count ≤ `capacity`.
- A never-released slot is reclaimed once its member score < now (no reaper needed).
- `wait_hint_seconds > 0` whenever `granted is False`.
- All keys carry a positive TTL after every acquire.
- Two different `workspace_id`s never contend on the same key.
