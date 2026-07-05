"""Buffered per-method attempt stats — atomic record + drain
(`contracts/stats-buffer.md`, FR-009, FR-022..FR-025, US5).

Pure Redis logic — the exact shape of `app_shared/access/budget.py`: a
`redis.Redis`-shaped client parameter, stdlib otherwise. **No**
Scrapy/Twisted/FastAPI/SQLAlchemy import (grep-enforced by T042's
import-boundary test and T038's reactor-safety proof). Flush-to-Postgres
lives in `app_shared/strategy/flush.py` (SQLAlchemy) + the worker task
(`apps/workers/app/workers/tasks_strategy.py`), never here.

## Keys (contracts/stats-buffer.md §Keys, data-model.md §6)

- `stratstat:{profile_id}:{method_type}:{method_name}` — HASH: `attempt`,
  `success`, `failure`, `rt_ms_sum`, `conf_sum` (raw/all-successes
  counters, driving `success_rate`/`avg_response_time_ms`/`avg_confidence`),
  plus `qual_success` — a **separate** counter incremented only on a
  *qualifying* success (contracts/promotion.md "Note": "the qualifying
  count is tracked as its own buffered counter ... implementation keeps
  a `HINCRBY qual_success` field so the count and the distinct-URL
  SCARD are independently checkable"). A non-qualifying success still
  `HINCRBY`s `success` (for `success_rate`) but never `qual_success` and
  is never `SADD`-ed to the URL set — so it cannot drive promotion
  (contracts/promotion.md "Qualifying success").
- `straturl:{profile_id}:{method_type}:{method_name}` — SET of distinct
  qualifying-success URL fingerprints (`sha1(normalize_url(url))` hex).
  Survives `drain` (only `SCARD`'d, never deleted there) — the running
  distinct-URL promotion evidence is only cleared by the flush task once
  the method actually promotes (`contracts/stats-buffer.md` "Drain").
- `stratdirty:{workspace_id}` — SET of profile ids with pending deltas,
  so the flush task enumerates dirty profiles without scanning every
  `stratstat:*` key.

TTL `STRATEGY_STATS_KEY_TTL_SECONDS` (`PEXPIRE`) is refreshed on every
touched key on every write -> a crashed writer's buffer self-evicts
(mirrors the SPEC-11 `bucket.py`/`locks.py` TTL discipline).

## This recorder stays narrow (FR-020a)

`record_attempt` is success/failure/response-time/confidence/URL only —
it is **not** widened with per-error-code/currency/template signals.
Rediscovery's per-attempt-outcome conditions (3, 5, 6, 7, 8) read those
off the hot path from `request_attempts` via
`app_shared.strategy.rediscovery.build_recent_signals` instead
(contracts/rediscovery.md "Two signal sources").

## Fail-open recording, mirroring `access/budget.py`

`record_attempt` never raises: any Redis error is logged and swallowed
(best-effort telemetry — a lost increment must never fail a scrape,
contracts/stats-buffer.md step 4). This is the same "fail open" posture
as `app_shared/access/budget.py` and deliberately differs from the
fail-closed rate-limiter/match-lock primitives (`app_shared/limiter/`),
whose *acquire* correctness is safety-critical in a way a stats counter
is not.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app_shared.url_pattern import normalize_url

__all__ = [
    "DrainedDelta",
    "PendingDelta",
    "dirty_key",
    "drain",
    "read_pending",
    "record_attempt",
    "url_key",
]

logger = logging.getLogger(__name__)

#: `stratstat:{profile_id}:{method_type}:{method_name}` HASH field names.
_ATTEMPT = "attempt"
_SUCCESS = "success"
_FAILURE = "failure"
_RT_MS_SUM = "rt_ms_sum"
_CONF_SUM = "conf_sum"
_QUAL_SUCCESS = "qual_success"

#: Scale factor `confidence` (a `Decimal`/`float` in `[0, 1]`) is multiplied
#: by before `HINCRBY` — Redis hash counters are integers only
#: (contracts/stats-buffer.md §Keys).
_CONFIDENCE_SCALE = 10_000

# Marker comment identifies this script to a test double's fake
# `register_script` (mirrors `app_shared/limiter/bucket.py`'s
# `-- SPEC-11 T0XX` convention); real Redis treats `--` as a plain Lua
# comment.
_DRAIN_LUA = """
-- stats-buffer.md drain (SPEC-12 T034): HGETALL+DEL the stat hash in one
-- round-trip so no concurrent writer's HINCRBY is lost between a
-- separate read and delete; SCARD (never DEL) the url set -- the
-- distinct-URL promotion evidence survives until the method promotes.
local stat_fields = redis.call('HGETALL', KEYS[1])
redis.call('DEL', KEYS[1])
local distinct_urls = redis.call('SCARD', KEYS[2])
return {stat_fields, distinct_urls}
"""

#: Module-level script cache — "register the script once" (mirrors
#: `bucket.py`/`locks.py`'s `_call_script`/module-global convention).
#: Content-addressed (SHA1 of the Lua source) inside real Redis, so it is
#: safe to register once against the first client seen and invoke it
#: against any later client via an explicit `client=` override.
_drain_script: Any = None


def _stat_key(profile_id: uuid.UUID | str, method_type: Any, method_name: str) -> str:
    return f"stratstat:{profile_id}:{_method_type_value(method_type)}:{method_name}"


def url_key(profile_id: uuid.UUID | str, method_type: Any, method_name: str) -> str:
    """`straturl:{profile_id}:{method_type}:{method_name}` -- exposed
    (not `_`-prefixed) so `app_shared/strategy/flush.py` can `DELETE` it
    once a method actually promotes (contracts/stats-buffer.md "Drain":
    the distinct-URL SET survives every `drain` call and is only cleared
    at that point) without reaching into this module's private key
    builders."""
    return f"straturl:{profile_id}:{_method_type_value(method_type)}:{method_name}"


def dirty_key(workspace_id: uuid.UUID | str) -> str:
    """`stratdirty:{workspace_id}` -- exposed so the flush task/`flush.py`
    can enumerate/`SREM` dirty profile ids without reaching into this
    module's private key builders."""
    return f"stratdirty:{workspace_id}"


def _method_type_value(method_type: Any) -> str:
    """`method_type.value` for an enum member, else the value itself
    (accepts a plain string too, e.g. from a test double or a caller that
    already unwrapped the enum) -- never raises on a well-formed string."""
    return method_type.value if hasattr(method_type, "value") else str(method_type)


def _fingerprint(url: str) -> str:
    """`sha1(normalize_url(url))` hex -- a bounded-size distinct-URL
    fingerprint (contracts/stats-buffer.md §Keys). Reuses the shipped
    identity-URL normalizer (`app_shared.url_pattern.normalize_url`) so
    two URLs that already dedupe at the match layer also dedupe here."""
    return hashlib.sha1(normalize_url(url).encode("utf-8"), usedforsecurity=False).hexdigest()


def _as_int(value: Any) -> int:
    """`0` for a missing/`None` Redis hash field, else `int(value)` --
    every `HGETALL` field comes back as a string (or bytes) from a real
    Redis client, never pre-parsed."""
    if value is None:
        return 0
    return int(value)


@dataclass(frozen=True)
class PendingDelta:
    """Non-destructive snapshot of a `(profile, method_type, method_name)`
    key's buffered delta (contracts/stats-buffer.md `read_pending`) --
    promotion/rediscovery add this to the persisted row **without**
    draining (FR-024)."""

    attempt: int
    success: int
    failure: int
    rt_ms_sum: int
    conf_sum: int
    qualifying_success: int
    distinct_urls: int


@dataclass(frozen=True)
class DrainedDelta:
    """Atomic read-and-reset result of `drain` (contracts/stats-buffer.md
    `drain`) -- the stat HASH is emptied; `distinct_urls` is a read-only
    `SCARD` (the url SET itself is left intact, contracts/stats-buffer.md
    "Distinct-url fingerprints ... are not deleted until the method is
    promoted")."""

    attempt: int
    success: int
    failure: int
    rt_ms_sum: int
    conf_sum: int
    qualifying_success: int
    distinct_urls: int


def record_attempt(
    redis: Any,
    *,
    workspace_id: uuid.UUID | str,
    profile_id: uuid.UUID | str,
    method_type: Any,
    method_name: str,
    success: bool,
    response_time_ms: int | None,
    confidence: Any | None,
    url: str,
    qualifying: bool,
    ttl_seconds: int,
) -> None:
    """Atomically buffer one attempt's outcome (contracts/stats-buffer.md
    `record_attempt`, O(1), no read-modify-write in Python):

    1. `HINCRBY attempt 1`; `HINCRBY success/failure 1` (whichever
       `success` selects); `HINCRBY rt_ms_sum response_time_ms` when a
       response time was measured; on `success`, `HINCRBY conf_sum
       int(confidence * 10000)` when a confidence was measured.
    2. If `qualifying` (the caller has already evaluated confidence >=
       `STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD` AND a valid numeric
       price AND currency-valid-when-required, contracts/promotion.md):
       `HINCRBY qual_success 1` and `SADD straturl:... sha1(url)`.
    3. `SADD stratdirty:{workspace_id} profile_id`.
    4. `PEXPIRE` every key touched this call to `ttl_seconds` -- a
       crashed writer's buffer self-evicts.

    **Called only from** the scraping pipeline's batched persistence flush
    (`_flush_batch`, already inside `run_in_thread`, off-reactor -- FR-025,
    SC-007; T038 asserts this is the only reachable call site). Any Redis
    error is logged and
    swallowed -- recording is best-effort telemetry; a lost increment
    must never fail a scrape (contracts/stats-buffer.md step 4, mirrors
    `app_shared/access/budget.py`'s fail-open posture).
    """
    try:
        stat_key = _stat_key(profile_id, method_type, method_name)
        this_url_key = url_key(profile_id, method_type, method_name)
        this_dirty_key = dirty_key(workspace_id)
        ttl_ms = ttl_seconds * 1000

        touched_keys = [stat_key]

        redis.hincrby(stat_key, _ATTEMPT, 1)
        redis.hincrby(stat_key, _SUCCESS if success else _FAILURE, 1)
        if response_time_ms is not None:
            redis.hincrby(stat_key, _RT_MS_SUM, int(response_time_ms))
        if success and confidence is not None:
            redis.hincrby(stat_key, _CONF_SUM, int(float(confidence) * _CONFIDENCE_SCALE))

        if qualifying:
            redis.hincrby(stat_key, _QUAL_SUCCESS, 1)
            redis.sadd(this_url_key, _fingerprint(url))
            touched_keys.append(this_url_key)

        redis.sadd(this_dirty_key, str(profile_id))
        touched_keys.append(this_dirty_key)

        for key in touched_keys:
            redis.pexpire(key, ttl_ms)
    except Exception:  # noqa: BLE001 - logged + swallowed, best-effort (contracts/stats-buffer.md step 4)
        logger.warning(
            "app_shared.strategy.stats_buffer: record_attempt failed for "
            "profile_id=%s method_type=%s method_name=%s",
            profile_id,
            method_type,
            method_name,
            exc_info=True,
        )


def read_pending(
    redis: Any,
    *,
    profile_id: uuid.UUID | str,
    method_type: Any,
    method_name: str,
) -> PendingDelta:
    """Non-destructive `HGETALL` + `SCARD` snapshot of the pending buffer
    (contracts/stats-buffer.md `read_pending`) -- used by promotion/
    rediscovery to add the pending delta to the persisted row **without**
    draining (FR-024). An absent hash/set (no pending activity, or the
    keys already expired/were never written) reads back as all-zero
    (never raises)."""
    stat_key = _stat_key(profile_id, method_type, method_name)
    this_url_key = url_key(profile_id, method_type, method_name)

    fields = redis.hgetall(stat_key) or {}
    # A real `redis.Redis` (with `decode_responses=True`, the project
    # convention -- `app_shared.redis_client.get_redis_client`) returns
    # `str` keys; a test double may use plain `str` too -- either way,
    # normalize defensively so a `bytes`-keyed client still works.
    normalized = {
        (key.decode("utf-8") if isinstance(key, bytes) else key): value
        for key, value in fields.items()
    }
    distinct_urls = redis.scard(this_url_key) or 0

    return PendingDelta(
        attempt=_as_int(normalized.get(_ATTEMPT)),
        success=_as_int(normalized.get(_SUCCESS)),
        failure=_as_int(normalized.get(_FAILURE)),
        rt_ms_sum=_as_int(normalized.get(_RT_MS_SUM)),
        conf_sum=_as_int(normalized.get(_CONF_SUM)),
        qualifying_success=_as_int(normalized.get(_QUAL_SUCCESS)),
        distinct_urls=int(distinct_urls),
    )


def drain(
    redis: Any,
    *,
    profile_id: uuid.UUID | str,
    method_type: Any,
    method_name: str,
) -> DrainedDelta:
    """Atomic read-and-reset of the stat HASH via a single Lua `EVAL`
    (registered once, `register_script` -- the SPEC-11 `bucket.py`/
    `locks.py` pattern): `HGETALL`+`DEL` the stat hash, `SCARD` (never
    delete) the url set, in one round-trip so no concurrent writer's
    `HINCRBY` is lost between a separate read and delete
    (contracts/stats-buffer.md `drain`). The url SET is deliberately left
    intact -- it is the running distinct-URL promotion evidence and is
    only cleared by the flush task once the method actually promotes.
    """
    global _drain_script
    stat_key = _stat_key(profile_id, method_type, method_name)
    this_url_key = url_key(profile_id, method_type, method_name)

    if _drain_script is None:
        _drain_script = redis.register_script(_DRAIN_LUA)
    raw_fields, distinct_urls = _drain_script(keys=[stat_key, this_url_key], args=[], client=redis)

    # `HGETALL` via Lua returns a flat [field, value, field, value, ...]
    # array (Redis's native reply shape for a table built from pairs).
    flat = list(raw_fields or [])
    fields: dict[str, str] = {}
    for i in range(0, len(flat) - 1, 2):
        key = flat[i]
        fields[key.decode("utf-8") if isinstance(key, bytes) else key] = flat[i + 1]

    return DrainedDelta(
        attempt=_as_int(fields.get(_ATTEMPT)),
        success=_as_int(fields.get(_SUCCESS)),
        failure=_as_int(fields.get(_FAILURE)),
        rt_ms_sum=_as_int(fields.get(_RT_MS_SUM)),
        conf_sum=_as_int(fields.get(_CONF_SUM)),
        qualifying_success=_as_int(fields.get(_QUAL_SUCCESS)),
        distinct_urls=int(distinct_urls or 0),
    )
