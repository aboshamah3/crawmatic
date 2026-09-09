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

F15 (EPA core-production-readiness, 2026-09-07): this client previously
carried no ``socket_timeout``/``socket_connect_timeout`` at all, so a
wedged Redis connection could block a caller (and, on the sync client
used from inside async request handlers, the whole event loop)
indefinitely. It now reads ``socket_timeout``, ``socket_connect_timeout``
and a ``health_check_interval`` from ``REDIS_SOCKET_TIMEOUT_SECONDS``
(default ``2.0``) / ``REDIS_CONNECT_TIMEOUT_SECONDS`` (default ``1.0``).

CONFIG NOTE: those two are read from ``os.environ`` rather than
``app_shared.config.Settings`` -- B7 ran in parallel with another worker
holding ``config.py`` for the run this landed in. They belong there as
typed settings; this is a placeholder until that consolidation lands
(see ``reports/B7.md``).
"""

from __future__ import annotations

import os

import redis

from app_shared.config import get_settings
from app_shared.redis_policy import enforce_redis_memory_policy

_redis_client: redis.Redis | None = None

#: See the CONFIG NOTE above: placeholder env-var reads until these are
#: consolidated into `app_shared.config.Settings`.
REDIS_SOCKET_TIMEOUT_SECONDS = float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "2.0"))
REDIS_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("REDIS_CONNECT_TIMEOUT_SECONDS", "1.0"))
#: Fixed, not (yet) a separate setting -- the criteria names it alongside
#: the two above without a distinct env var; a value comfortably inside
#: the socket timeout keeps a dead connection from going unnoticed
#: between requests.
_REDIS_HEALTH_CHECK_INTERVAL_SECONDS = 30


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
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            health_check_interval=_REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
        )
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
