# Contract: Buffered Attempt Stats — record + atomic flush (FR-009, FR-022..FR-025)

**Module**: `app_shared/strategy/stats_buffer.py` — pure Redis logic, `redis.Redis`-shaped client
parameter, stdlib otherwise. **No** Scrapy/Twisted/FastAPI/SQLAlchemy import (the exact
`app_shared/access/budget.py` shape). Flush-to-Postgres lives in `app_shared/strategy/flush.py`
(SQLAlchemy) + the worker task.

## Keys (research D4, data-model §6)

- `stratstat:{profile_id}:{method_type}:{method_name}` — HASH: `attempt`, `success`, `failure`,
  `rt_ms_sum`, `conf_sum` (integer counters; `conf_sum` scaled to int, e.g. confidence×10000).
- `straturl:{profile_id}:{method_type}:{method_name}` — SET of distinct qualifying-success URL
  fingerprints (`sha1(normalized_url)` hex — bounded key size).
- `stratdirty:{workspace_id}` — SET of profile ids with pending deltas.
- TTL `STRATEGY_STATS_KEY_TTL_SECONDS` (`PEXPIRE`) on every key on every write → crashed-writer
  self-eviction (mirrors SPEC-11 TTL discipline).

## `record_attempt(redis, *, workspace_id, profile_id, method_type, method_name, success, response_time_ms, confidence, url, qualifying)`

Atomic, O(1), **no read-modify-write in Python** (§14):
1. `HINCRBY attempt 1`; `HINCRBY success/failure 1`; `HINCRBY rt_ms_sum response_time_ms`;
   on success `HINCRBY conf_sum int(confidence*10000)`.
2. If `qualifying` (success ∧ confidence ≥ threshold ∧ valid numeric price ∧ valid currency-when-required
   — evaluated by the caller): `SADD straturl:… sha1(url)`.
3. `SADD stratdirty:{workspace_id} profile_id`; `PEXPIRE` all touched keys.
4. Any Redis error is **logged and swallowed** (recording is best-effort telemetry; a lost increment
   must never fail a scrape — divergent from the fail-closed limiter, matching budget.py's tolerance).

**Called only from** `scrape_core.pipelines._flush_batch` (already inside `run_in_thread`, off-reactor —
FR-025, SC-007). One `record_attempt` per `RequestAttempt`/`PriceObservation` pair being persisted,
**only when that match's group resolved a `profile_id`** (D5). The reactor-safety AST grep test
(`tests/unit/test_reactor_safety_grep.py`) continues to prove no such Redis call runs on the reactor.

## `read_pending(redis, *, profile_id, method_type, method_name) -> PendingDelta`

Non-destructive `HGETALL` + `SCARD` → `(attempt, success, failure, rt_ms_sum, conf_sum, distinct_urls)`.
Used by promotion/rediscovery to add pending deltas to the persisted row (FR-024) **without** draining.

## `drain(redis, *, profile_id, method_type, method_name) -> DrainedDelta`

Atomic **read-and-reset** via a single Lua `EVAL` (registered once, `register_script`, SPEC-11 pattern):
`HGETALL` the stat hash, `SCARD`+`SMEMBERS`(or just `SCARD`) the url set, then `DEL` both — returning the
delta and the distinct-url count in one round-trip so no concurrent writer's increment is lost between a
separate read and delete. Distinct-url fingerprints that must survive across flush cycles (the SET is the
running distinct-URL evidence for promotion) are **not** deleted until the method is promoted — so `drain`
deletes only the `stratstat` hash and reads (not deletes) `SCARD straturl`. Returns
`DrainedDelta(attempt, success, failure, rt_ms_sum, conf_sum, distinct_urls)`.

## Flush to Postgres — `flush_profile(session, redis, profile_id)` (`app_shared/strategy/flush.py`)

Runs in the `STRATEGY_STATS_FLUSH` Celery task (off-reactor worker) and at job finalization. For each
`(method_type, method_name)` key of a dirty profile:
1. `drain(...)` the Redis delta.
2. **Single atomic UPDATE per key** (FR-023, no app-side RMW), upserting the stats row:
   ```sql
   INSERT INTO strategy_attempt_stats (id, domain_strategy_profile_id, method_type, method_name,
       attempt_count, success_count, failure_count, rt_ms_sum?, ...)
   VALUES (:uuid7, :pid, :mt, :mn, :d_att, :d_suc, :d_fail, ...)
   ON CONFLICT (domain_strategy_profile_id, method_type, method_name) DO UPDATE SET
       attempt_count = strategy_attempt_stats.attempt_count + EXCLUDED.attempt_count,
       success_count = strategy_attempt_stats.success_count + EXCLUDED.success_count,
       failure_count = strategy_attempt_stats.failure_count + EXCLUDED.failure_count,
       success_rate  = (new success_count) / NULLIF(new attempt_count, 0),
       avg_response_time_ms = running_avg, avg_confidence = running_avg,
       last_success_at = GREATEST(...), last_failed_at = GREATEST(...);
   ```
   (`avg_*` recomputed from persisted running sums; the sums may be carried in extra internal columns or
   recomputed — implementation detail for tasks.md, but the value written is `count = count + delta`.)
3. Update the profile: `recent_failure_count` (++ on a preferred-method failure delta, reset to 0 on a
   qualifying-success delta — Clarification #2), `last_success_at`/`last_failed_at`.
4. Evaluate **promotion** (`contracts/promotion.md`) and **rediscovery** (`contracts/rediscovery.md`) on
   the just-flushed profile with **persisted + still-pending** counts.
5. `SREM stratdirty:{workspace_id} profile_id` once no pending deltas remain.

## Guarantees

- N attempts on one hot domain → **0** per-attempt stats-row writes; ≤1 UPDATE per (profile, method) key
  per flush interval, independent of N (SC-003).
- Promotion/rediscovery never read a stale count (persisted + pending, FR-024).
- No blocking Redis/DB on the reactor (recording inside `_flush_batch`; flush in Celery — FR-025, SC-007).
