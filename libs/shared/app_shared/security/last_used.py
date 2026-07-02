"""API-key ``last_used_at`` write throttle (`contracts/security-cache.md`, FR-015/SC-008).

Framework-agnostic helper taking a sync ``redis.Redis``-shaped client (or
any object exposing ``set``/``get`` with the same semantics — e.g. a fake
in tests). Lives on the correctness-critical ``noeviction`` Redis
instance (§4). Best-effort: usage tracking must never block a request or
risk a duplicate/racy DB write, so any Redis error fails safe to "skip
the write" rather than "write every time" or raise.
"""

from __future__ import annotations


def should_write_last_used(redis: object, *, key_id: object, throttle_seconds: int) -> bool:
    """Return ``True`` iff the caller should perform the ``last_used_at`` UPDATE.

    Uses the atomic gate ``SET apikey:lastused:{key_id} 1 NX EX
    throttle_seconds``: the ``SET`` only succeeds (returns truthy) when
    the key was previously absent, so this returns ``True`` at most once
    per ``throttle_seconds`` window per key — the caller performs the
    single ``UPDATE api_keys SET last_used_at = now()`` only on that
    ``True`` (FR-015/SC-008, ≤1 write/key/min regardless of request
    volume). Every other call within the window returns ``False`` (no
    write).

    **Fail-safe**: any Redis error returns ``False`` — usage tracking is
    best-effort and must never block or duplicate the request.
    """
    gate_key = f"apikey:lastused:{key_id}"
    try:
        result = redis.set(gate_key, 1, nx=True, ex=throttle_seconds)  # type: ignore[attr-defined]
        return bool(result)
    except Exception:
        return False
