# Contract: Redis budget & rate ceilings (`app_shared.access.budget`)

Framework-agnostic; takes a `redis.Redis`-shaped client (like `security/rate_limit.py`). All
keys live on the correctness-critical `noeviction` Redis. Enforces FR-010 (proxy monthly
budget) and FR-011 (policy ceilings + per-domain cooldown) with cheap counters — **never** a
`request_attempts` row scan (§22, Principle VIII).

## Settings (append to `app_shared/config.py`)

```python
# --- Access ceilings / resolution cache (SPEC-10 FR-010/011, §9/§22) ---
ACCESS_RESOLUTION_CACHE_TTL_SECONDS: int = 30
```

(Ceilings/cooldown values are per-policy / per-domain DB columns, not global settings.)

## API

```python
@dataclass(frozen=True)
class BudgetResult:
    allowed: bool          # False once the increment exceeds monthly limit
    used: int
    limit: int | None

def incr_and_check_monthly_budget(redis, *, provider_id, limit, now) -> BudgetResult:
    """Key proxybudget:{provider_id}:{now:%Y_%m}. INCR; on first hit (==1) EXPIRE to the end
    of the month (seconds until next month start). limit is None -> always allowed (no cap).
    Incremented once per PROXIED request only. Approximate under contention is acceptable
    (soft ceiling with a defined fallback, Assumptions). Redis error -> allowed=True
    (fail-open: a budget outage must not wedge scraping; the target is still fetched direct
    per strategy)."""

@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: int

def check_rate_ceilings(redis, *, policy_id, domain, per_minute, per_hour, per_day) -> RateDecision:
    """Up to three windowed INCR+EXPIRE counters (60/3600/86400 s), keyed
    ratelimit:{policy_id}:{domain}:{window}. Any None ceiling is skipped. Over any ceiling ->
    allowed=False with retry_after = that window's remaining TTL, mapped by the caller to
    RATE_LIMITED (FR-011). Fail-SAFE (deny) on Redis error for the rate path is NOT used here
    — instead fail-open so a Redis blip does not silently drop legitimate scrapes; the
    cluster-wide hard limiter (SPEC-11) owns strict enforcement. (Documented divergence from
    login rate-limit which fails closed.)"""

def check_domain_cooldown(redis, *, domain, cooldown_seconds) -> bool:
    """SET NX EX gate: cooldown:{domain}. Returns True if the request may proceed (gate
    acquired), False if still cooling down. cooldown_seconds<=0 -> always True."""
```

Per-domain `max_concurrent_requests` is expressed as **intent** only (a bounded semaphore key
sketch); the fencing-token, cluster-wide concurrency semaphore is SPEC-11 and is explicitly
out of scope (spec Edge Cases + Assumptions).

## Integration points

- Budget: `incr_and_check_monthly_budget` is called (off-reactor, `run_in_thread`) right
  before a `PROXY_HTTP`/`PLAYWRIGHT_PROXY` attempt is dispatched; on `allowed=False` the caller
  feeds `proxy_budget_exhausted=True` into `next_attempt` (fall back per strategy or
  `LIMIT_REACHED`).
- Ceilings/cooldown: checked before dispatching an attempt for a domain; `allowed=False` ->
  defer/skip the target with `RATE_LIMITED` recorded on its `RequestAttempt`.

## Acceptance (unit with a fake redis + integration skip-clean)

- `incr_and_check_monthly_budget`: the (limit+1)-th proxied increment in a month returns
  `allowed=False`; a new `%Y_%m` key (simulated `now`) resets; TTL set only on first hit.
- Budget is never derived from `request_attempts` (there is no such query in the module).
- `check_rate_ceilings`: exceeding per-minute flips `allowed=False` with a positive
  `retry_after`; independent windows tracked separately.
- `check_domain_cooldown`: second call within `cooldown_seconds` returns False; after expiry
  True.
