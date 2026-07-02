# Contract: Redis security helpers (`app_shared/security/{rate_limit,status_cache,last_used}.py`)

Framework-agnostic helpers taking a sync `redis.Redis` client. **All keys live on the correctness-critical `noeviction` Redis instance** (§4). Security-sensitive helpers **fail safe (deny/challenge)** when Redis is unavailable (spec Edge Case / Assumptions).

## Login rate limiter — `rate_limit.py`

```python
def check_and_increment_login(redis, *, email: str, source_ip: str,
                              max_attempts: int, window_seconds: int) -> RateLimitResult: ...
```

- **Keys**: `rl:login:acct:{sha256(email)}`, `rl:login:src:{source_ip}` (INCR + `EXPIRE window_seconds`); backoff lock `rl:login:lock:{scope}` whose TTL grows with repeated violations (progressive backoff).
- **Refuse if either** the account **or** the source is over `max_attempts` (FR-007/SC-009).
- **Fail-safe**: any Redis error → treat as limited (deny/challenge), never allow unlimited attempts.
- Uniform response: the throttled result carries **no factor disclosure** (FR-006/SC-009).

## Status cache — `status_cache.py`

```python
def get_user_status(redis, session_factory, user_id) -> str: ...
def get_workspace_status(redis, session_factory, workspace_id) -> str: ...
def invalidate_user(redis, user_id) -> None
def invalidate_workspace(redis, workspace_id) -> None
```

- **Keys**: `status:user:{user_id}`, `status:ws:{workspace_id}` → status string, TTL `STATUS_CACHE_TTL_SECONDS` (default 30, ~30–60s).
- **Hit** → return cached (no DB). **Miss** → single DB read, repopulate with TTL. Steady state → **0 per-request status DB reads** (FR-022/SC-007).
- A suspension takes effect within one TTL (SC-007). Explicit `invalidate_*` may be called on suspend for immediacy.
- **Fail-safe**: Redis error → deny (treat as not-active) rather than assume active.

## API-key last-used throttle — `last_used.py`

```python
def should_write_last_used(redis, *, key_id, throttle_seconds) -> bool: ...
```

- **Gate key**: `apikey:lastused:{key_id}` set with `SET key 1 NX EX throttle_seconds` (default 60).
- Returns `True` **only** when the `SET NX` succeeds (gate absent) → caller performs the single `UPDATE api_keys SET last_used_at = now()`; otherwise `False` → no write. → **≤1 write/key/min regardless of volume** (FR-015/SC-008, 0 per-request writes).
- **Fail-safe**: Redis error → return `False` (skip the update; usage tracking is best-effort, never blocks the request or risks a duplicate write).

## Tests

- Unit: helper logic with a fake/in-memory Redis (SET NX gate returns True once then False within the window; rate-limit refuses over threshold; fail-safe on injected client error).
- Live (`test_rate_limit.py`, `test_status_cache.py`, `test_last_used_throttle.py`): real Redis — backoff engages; suspend→rejected within TTL and 0 per-request status DB reads; ≤1 last_used write/key/min under burst.
