"""In-flight match lock (fencing token) Redis primitives
(contracts/match-lock.md; FR-010..FR-014, FR-016, D6).

Pure Redis logic — stdlib ``secrets`` + an injected ``redis.Redis``-shaped
client; **no** Scrapy/Twisted/FastAPI import (a sibling of
``app_shared/limiter/bucket.py`` and ``app_shared/access/budget.py``).

**Ownership**: the acquirer generates a unique fencing token
(:func:`new_fencing_token`) and threads it through to the releaser (the
spider's persistence pipeline, after the observation/attempt write
commits). The Lua compare-and-delete on release guarantees a slow prior
owner (e.g. a crashed/very-slow worker whose lock already expired and
was re-acquired by a fresh owner) can never delete a *newer* owner's
lock — its stale token simply no longer matches (FR-012, US2 AS3).

**Fail-closed** on acquire (FR-023, D3 — same policy as
``app_shared/limiter/bucket.py``): a Redis error during acquire is
treated as "cannot confirm ownership" -> do not fetch (``False``).
Release errors are logged and swallowed — a TTL always reclaims the key
regardless, so a release failure never crashes persistence (D3).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["acquire_match_lock", "new_fencing_token", "release_match_lock"]

# Marker comment identifies this script to a test double's fake
# `register_script` (mirrors `app_shared/limiter/bucket.py`'s
# `-- SPEC-11 T0XX` convention); real Redis treats `--` as a plain Lua
# comment.
_RELEASE_LUA = """
-- match-lock.md compare-and-delete release (SPEC-11 T019)
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

#: Module-level script cache — "register the script once" (mirrors
#: `bucket.py`'s `_call_script` convention). Content-addressed (SHA1 of
#: the Lua source) inside real Redis, so it is safe to register once
#: against the first client seen and invoke it against any later client
#: via an explicit `client=` override.
_release_script: Any = None


def new_fencing_token() -> str:
    """A unique 128-bit fencing token (``secrets.token_hex(16)``)."""
    return secrets.token_hex(16)


def acquire_match_lock(redis: Any, *, key: str, token: str, ttl_seconds: int) -> bool:
    """``SET key token NX PX ttl_seconds``.

    ``True`` = we now own the lock. ``False`` = the match is already
    being scraped (``NX`` failed -- caller marks the target ``SKIPPED``/
    ``LOCKED_ALREADY_RUNNING`` and does **no** fetch, FR-014) **or** a
    Redis error occurred (fail-closed -- "cannot confirm ownership" is
    treated as do-not-fetch, FR-023, D3).
    """
    try:
        acquired = redis.set(key, token, nx=True, px=ttl_seconds * 1000)
    except Exception:  # noqa: BLE001 - fail-closed on any Redis error (FR-023)
        return False
    return bool(acquired)


def release_match_lock(redis: Any, *, key: str, token: str) -> bool:
    """Atomic Lua compare-and-delete: ``DEL`` only if ``GET key == token``.

    ``True`` = we released our own lock. ``False`` = the stored token no
    longer matches ours (an expired-then-reacquired lock owned by a new
    holder is preserved -- our release is correctly a no-op, US2 AS3) or
    a Redis error occurred. Redis errors are logged and swallowed --
    never raised (a TTL always reclaims the key regardless, D3).
    """
    global _release_script
    try:
        if _release_script is None:
            _release_script = redis.register_script(_RELEASE_LUA)
        result = _release_script(keys=[key], args=[token], client=redis)
    except Exception:  # noqa: BLE001 - logged + swallowed, TTL reclaims (D3)
        logger.warning(
            "app_shared.limiter.locks: release_match_lock failed for key=%s", key, exc_info=True
        )
        return False
    return bool(result)
