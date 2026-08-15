"""Lazy, per-process Redis client (framework-agnostic).

Mirrors the lazy-singleton pattern in ``app_shared.database`` (SPEC-01):
the client is built on first use from ``Settings.REDIS_URL`` and cached
per-process — **never** at import time (would defeat fail-fast config
validation) and **never** per-request (would leak connections). Consumed
by the SPEC-03 security primitives (``rate_limit``, ``status_cache``,
``last_used``) that take a ``redis.Redis``-shaped client as a parameter,
and by ``apps/api`` routers/dependencies that construct one to pass in.

All security-sensitive callers are responsible for their own fail-safe
handling on connection errors (per contracts/security-cache.md) — this
module only owns connectivity, not policy.
"""

from __future__ import annotations

import redis

from app_shared.config import get_settings
from app_shared.redis_policy import enforce_redis_memory_policy

_redis_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    """Return the per-process Redis client, creating it on first use.

    On the **first** creation in a process the connected server's
    ``maxmemory-policy`` is asserted (audit 2026-08-15 risk H2, see
    ``app_shared.redis_policy``): this instance holds correctness- and
    cost-critical keys (match locks, dispatch sentinels, ``proxybudget:*``
    spend counters), so an eviction-capable policy is a confirmed
    misconfiguration and the process refuses to start. A server that
    cannot answer ``CONFIG GET`` is only warned about, never fatal, and
    ``PROXY_REDIS_REQUIRE_NOEVICTION=false`` disables enforcement
    entirely -- see that module's docstring for the full rationale.
    """
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        enforce_redis_memory_policy(
            client, require=settings.PROXY_REDIS_REQUIRE_NOEVICTION
        )
        _redis_client = client
    return _redis_client


def dispose_redis_client() -> None:
    """Close and clear the cached client (fork-safety, mirrors ``dispose_engine``)."""
    global _redis_client
    if _redis_client is not None:
        _redis_client.close()
    _redis_client = None
