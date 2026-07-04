"""Reactor-safe seam over ``app_shared.limiter`` (contracts/reactor-seam.md).

DECISION OF RECORD (stated verbatim per contracts/reactor-seam.md — the
only place allowed to touch Twisted for this feature, Constitution V):

    The distributed rate-limiter, semaphore, and match-lock Redis
    round-trips are synchronous ``redis`` client ``EVAL``/``SET``/
    ``ZADD`` calls executed off the Twisted reactor via
    ``deferToThread`` (the existing ``scrape_core.db.run_in_thread``
    seam SPEC-07/10 use for every Redis/DB round-trip). No async-redis
    client is introduced. The wait between requeues is a non-blocking
    reactor ``callLater``-backed ``Deferred``
    (``scrape_core.reactor.deferred_delay``). There is no
    ``time.sleep`` and no synchronous Redis call on the reactor thread
    anywhere in the scrape path (FR-007, SC-005). Rationale in
    ``research.md`` D2.

This module wraps the pure ``app_shared.limiter`` functions (token
bucket, semaphore, fencing lock) so the spider/pipeline only ever
awaits a ``Deferred`` — never calls ``redis`` synchronously itself.
Every wrapper below is a stub for now; bodies are filled in per user
story (T012 fills ``acquire_permission``/``release_slot`` for US1,
T021 fills ``acquire_lock``/``release_lock`` for US2) without changing
these signatures.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.limiter import bucket as _bucket
from app_shared.limiter.keys import rate_key as _rate_key
from app_shared.limiter.keys import semaphore_key as _semaphore_key

from scrape_core.db import run_in_thread

#: Fixed wait hint (seconds) used when the token bucket grants but the
#: concurrency semaphore is full — the bucket carries no information
#: about how long the *semaphore* will stay saturated, and slots free
#: up frequently (fetch-scoped, not persist-scoped), so a short fixed
#: retry hint is sufficient (T012).
_SEMAPHORE_DENIAL_WAIT_HINT_SECONDS = 1.0

__all__ = [
    "LockGrant",
    "Permission",
    "acquire_lock",
    "acquire_permission",
    "release_lock",
    "release_slot",
]


@dataclass(frozen=True)
class Permission(object):
    """Outcome of an :func:`acquire_permission` call.

    ``granted`` reflects both the token bucket AND the semaphore
    (both must grant). ``wait_hint_seconds`` is only meaningful on
    denial. ``semaphore_key``/``semaphore_token`` are threaded through
    to the later :func:`release_slot` call and are only populated when
    a slot was actually acquired.
    """

    granted: bool
    wait_hint_seconds: float
    semaphore_key: str | None = None
    semaphore_token: str | None = None


@dataclass(frozen=True)
class LockGrant(object):
    """Outcome of a successful :func:`acquire_lock` call (``None`` on denial)."""

    key: str
    token: str


async def acquire_permission(
    redis: object,
    *,
    workspace_id: uuid.UUID | str,
    domain: str,
    access_method: object,
    limits: object,
    settings: object,
    sem_token: str,
) -> Permission:
    """Grant/deny an outbound-fetch permission (token bucket THEN semaphore).

    Both checks run via ``await run_in_thread(...)`` — never a
    synchronous Redis call on the reactor thread. The token bucket is
    checked first; if it denies, the semaphore is **never touched** and
    its denial's ``wait_hint_seconds`` is returned as-is. If the bucket
    grants but the semaphore is full, the token is *not* refunded
    (acceptable — the bucket self-refills; simpler and never
    over-grants) and a small fixed wait hint is returned. Any Redis
    error surfaces as ``granted=False`` (fail-closed), never a raised
    exception (reactor-seam.md).
    """
    ttl_seconds = 2 * 60 + settings.RATE_LIMIT_KEY_TTL_SLACK_SECONDS
    key = _rate_key(workspace_id, domain, access_method)
    bucket_result = await run_in_thread(
        _bucket.acquire_token,
        redis,
        key=key,
        capacity=limits.per_minute,
        ttl_seconds=ttl_seconds,
    )
    if not bucket_result.granted:
        return Permission(granted=False, wait_hint_seconds=bucket_result.wait_hint_seconds)

    sem_key = _semaphore_key(workspace_id, domain, access_method)
    key_ttl_seconds = settings.SEMAPHORE_SLOT_TTL_SECONDS + settings.RATE_LIMIT_KEY_TTL_SLACK_SECONDS
    slot_granted = await run_in_thread(
        _bucket.acquire_slot,
        redis,
        key=sem_key,
        limit=limits.concurrency,
        token=sem_token,
        slot_ttl_seconds=settings.SEMAPHORE_SLOT_TTL_SECONDS,
        key_ttl_seconds=key_ttl_seconds,
    )
    if not slot_granted:
        return Permission(granted=False, wait_hint_seconds=_SEMAPHORE_DENIAL_WAIT_HINT_SECONDS)

    return Permission(
        granted=True,
        wait_hint_seconds=0,
        semaphore_key=sem_key,
        semaphore_token=sem_token,
    )


async def release_slot(redis: object, *, key: str, token: str) -> None:
    """Release a previously-acquired semaphore slot (``ZREM``, off-reactor).

    Redis errors are logged and swallowed inside
    ``app_shared.limiter.bucket.release_slot`` (D3) — this never raises.
    """
    await run_in_thread(_bucket.release_slot, redis, key=key, token=token)


async def acquire_lock(
    redis: object,
    *,
    workspace_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    mode: object,
    settings: object,
) -> LockGrant | None:
    """Acquire the in-flight match lock, or ``None`` when already held.

    Stub — filled in by T021 (US2). Must build the mode-sized TTL,
    generate the fencing token, and call
    ``await run_in_thread(acquire_match_lock, ...)`` — fail-closed
    surfaces as ``None`` (reactor-seam.md).
    """
    raise NotImplementedError("acquire_lock is implemented in US2 (T021)")


async def release_lock(redis: object, *, key: str, token: str) -> None:
    """Release a previously-acquired match lock (fencing compare-and-delete).

    Stub — filled in by T021 (US2).
    """
    raise NotImplementedError("release_lock is implemented in US2 (T021)")
