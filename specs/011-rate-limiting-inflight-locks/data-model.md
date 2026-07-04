# Phase 1 Data Model: Distributed Rate Limiting & In-Flight Locks

This feature introduces **no new PostgreSQL table, column, or Alembic migration**. Its state
lives in **Redis** (three TTL-bounded key families) plus **one new VARCHAR enum member**. It
*reads* existing DB config and *writes* only through the existing target-status seam.

---

## 1. Redis entities (ephemeral, TTL-bounded, workspace-namespaced)

### 1.1 Rate-limit token bucket
- **Key**: `rate:{workspace_id}:{domain}:{ACCESS_METHOD}` where
  `ACCESS_METHOD ∈ {DIRECT_HTTP, PROXY_HTTP, PLAYWRIGHT_PROXY}` (FR-002).
- **Type**: Redis hash `{tokens: float, ts: float(server epoch)}`.
- **Fields / rules**:
  - `capacity` = effective `max_requests_per_minute` (a full minute burst).
  - `refill_rate` = `capacity / 60` tokens/sec, computed on the **Redis server clock** inside Lua (FR-004, clock-skew-safe).
  - Grant when `tokens ≥ 1` → decrement 1; else deny with `wait_hint = ceil((1 - tokens)/refill_rate)` (FR-006).
- **TTL**: `PEXPIRE` on every touch to `≈ 2 × window` (bounded; FR-005) so an idle bucket self-evicts and a crashed writer never leaves stale state.
- **Isolation**: `workspace_id` prefix ⇒ two workspaces on one domain never share a bucket (FR-009, US1 AS5).

### 1.2 Domain concurrency semaphore
- **Key**: `semaphore:{workspace_id}:{domain}:{access_method}` (FR-003).
- **Type**: Redis sorted set `{fencing_token → expiry_epoch}`.
- **Rules**: acquire (Lua) purges `ZREMRANGEBYSCORE(-inf, now)` expired holders, then grants iff `ZCARD < max_concurrent_requests` by `ZADD`-ing the caller's token; release = `ZREM` token.
- **TTL**: member score = `now + slot_ttl`; key `PEXPIRE`d to `slot_ttl + slack`. Expired members are reclaimed inside acquire ⇒ a crashed holder never permanently occupies a slot (FR-005, US1 AS4, SC-004).
- **Lifecycle**: held only for the **fetch** — acquired at dispatch, released when the response/failure returns (distinct from the match lock's fetch→persist span).

### 1.3 Match lock (in-flight dedup)
- **Key**: `lock:scrape:{workspace_id}:{match_id}` (FR-010).
- **Type**: string; **value = unique fencing token** (`secrets.token_hex(16)`).
- **Rules**: acquire = `SET key token NX PX ttl` (held ⇒ `NX` fails ⇒ skip, FR-014); release = Lua compare-and-delete (delete only if stored value == releaser token, FR-012, US2 AS2/AS3).
- **TTL (mode-sized, FR-013)**: HTTP `MATCH_LOCK_HTTP_TTL_SECONDS` (~600s); browser `MATCH_LOCK_BROWSER_TTL_SECONDS` (~1800s). Each ≫ worst-case in-spider wait (`REQUEUE_MAX_ATTEMPTS × max backoff`) so a working owner never loses its lock (US2 AS4).
- **Ownership**: spider acquires before fetch; spider's persistence pipeline releases after the write; token threaded via request `meta` → `ScrapeResult` (FR-011). `price_analysis` needs no lock (FR-016).

**Fail-closed** (all three, on acquire): any Redis error ⇒ *not granted* / *do not fetch* (FR-023, D3). Release swallows Redis errors (TTL reclaims).

---

## 2. Enum delta — `libs/shared/app_shared/enums.py`

### 2.1 `ScrapeTargetStatus` — ADD one member (VARCHAR; no migration)
```
PENDING | STARTED | COMPLETED | FAILED | SKIPPED | DEFERRED   # +DEFERRED (NEW)
```
- **`DEFERRED`**: an overflowed target (requeue cap exceeded) handed back to Celery for later
  re-dispatch (FR-018). **Non-terminal** — not added to `_TERMINAL_TARGET_STATUSES`; a job with
  a DEFERRED target stays open until re-dispatch resolves it (bounded by the job lifecycle —
  edge case "overflow loop").
- Rendered as VARCHAR via `enum_column` (StrEnum) ⇒ **no Alembic migration** (SPEC-08/10 precedent).

### 2.2 Reused, NOT added (verify only)
- `AccessMethod.{DIRECT_HTTP, PROXY_HTTP, PLAYWRIGHT_PROXY}` — already exist (used in keys).
- `ScrapeErrorCode.RATE_LIMITED` — overflow outcome (FR-020).
- `ScrapeErrorCode.LOCKED_ALREADY_RUNNING` — lock-collision SKIPPED (FR-021).

---

## 3. Reused DB schema (read-only — no change)

| Table / column | Role in SPEC-11 | Change |
|---|---|---|
| `DomainAccessRule.max_requests_per_minute` | token-bucket capacity (domain override) | none (read) |
| `DomainAccessRule.max_concurrent_requests` | semaphore limit (domain override) | none (read) |
| `DomainAccessRule.cooldown_seconds` | per-domain cooldown (domain override) | none (read) |
| `AccessPolicy.max_requests_per_minute` | token-bucket capacity fallback | none (read) |
| `scrape_job_targets.status` | gains `DEFERRED` value; SKIPPED for lock collision | value only (no DDL) |
| `scrape_job_targets.error_code` | RATE_LIMITED / LOCKED_ALREADY_RUNNING | none (reuse) |
| `unique(scrape_job_id, match_id)` on `scrape_job_targets` | prevents duplicate targets pre-lock | **verify** (SPEC-08; FR-015) |

Values come via the **already-cached SPEC-10 effective-policy resolution** the spider's
`load_targets` bounded-load already performs — no new query, no N+1 walk (Principle IV, D4).

---

## 4. Config / `Settings` knobs — `libs/shared/app_shared/config.py`

All env-tunable (Principle IV); guidance values from §12/§13.

| Setting | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | e.g. `60` | token-bucket capacity when no rule/policy value |
| `RATE_LIMIT_DEFAULT_CONCURRENCY` | e.g. `4` | semaphore limit when no rule value |
| `RATE_LIMIT_KEY_TTL_SLACK_SECONDS` | e.g. `120` | slack added to bucket/semaphore key TTL |
| `SEMAPHORE_SLOT_TTL_SECONDS` | e.g. `600` | per-slot hold TTL (auto-reclaim on crash) |
| `MATCH_LOCK_HTTP_TTL_SECONDS` | `600` | HTTP match-lock TTL (§13) |
| `MATCH_LOCK_BROWSER_TTL_SECONDS` | `1800` | browser match-lock TTL (§13) |
| `REQUEUE_MAX_ATTEMPTS` | e.g. `5` | max in-spider requeues per request (FR-017) |
| `REQUEUE_MAX_TOTAL_WAIT_SECONDS` | e.g. `300` | max cumulative in-spider wait per request (FR-017) |
| `RATE_LIMIT_JITTER_MIN_SECONDS` | `2` | lower jitter bound (FR-006, §12) |
| `RATE_LIMIT_JITTER_MAX_SECONDS` | `20` | upper jitter bound (FR-006, §12) |

Lock TTLs MUST comfortably exceed `REQUEUE_MAX_ATTEMPTS × (wait_hint + JITTER_MAX)` (FR-013,
US2 AS4) — validated as an invariant in unit tests.

---

## 5. State transitions — `scrape_job_targets.status`

```
                 dispatch (limiter+lock OK)          persist success
   PENDING ─────────────────────────────────► STARTED ──────────────► COMPLETED
      │  \                                        │   persist failure
      │   \  lock held                            └────────────────────► FAILED
      │    └──────────────────────────────► SKIPPED (LOCKED_ALREADY_RUNNING)
      │
      │  requeue cap exceeded (overflow)
      └──────────────────────────────────► DEFERRED ──enqueue scrape_dispatch──┐
                                              ▲                                 │
                                              └──── re-dispatch (later batch) ──┘
                                                    DEFERRED → STARTED, re-subject
                                                    to lock + limiter (no double-run, FR-019)
```

- `DEFERRED` and `SKIPPED` are the two new/expanded outcomes; `COMPLETED`/`FAILED`/`STARTED`/
  `PENDING` keep their SPEC-08 meaning.
- The overflowed `RATE_LIMITED` outcome is recorded on the target/attempt (FR-020); the
  lock-collision `SKIPPED` carries `LOCKED_ALREADY_RUNNING` (FR-021) — both via the existing
  `ScrapeResult → mark_target` path (no new writer).
