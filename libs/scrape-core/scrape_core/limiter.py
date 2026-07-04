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
``acquire_permission``/``release_slot`` (US1, T012) and
``acquire_lock``/``release_lock`` (US2, T021) are both filled in; every
wrapper's signature was fixed from the start (T006) so no later story
had to change an earlier one's call sites.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.enums import AccessMethod
from app_shared.limiter import bucket as _bucket
from app_shared.limiter import locks as _locks
from app_shared.limiter.keys import match_lock_key as _match_lock_key
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

    ``denied_by`` (SPEC-11 US4, T031) distinguishes *which* gate denied
    on a non-grant -- ``"bucket"`` (the token bucket itself denied, the
    semaphore was never touched) or ``"semaphore"`` (the bucket granted
    but the concurrency slot was full) -- so the spider can emit the
    correct one of ``rate_limit.hit``/``semaphore.denied``
    (`contracts/observability.md`) instead of conflating the two.
    ``None`` on a grant (meaningless there).
    """

    granted: bool
    wait_hint_seconds: float
    semaphore_key: str | None = None
    semaphore_token: str | None = None
    denied_by: str | None = None


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
        return Permission(
            granted=False, wait_hint_seconds=bucket_result.wait_hint_seconds, denied_by="bucket"
        )

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
        return Permission(
            granted=False,
            wait_hint_seconds=_SEMAPHORE_DENIAL_WAIT_HINT_SECONDS,
            denied_by="semaphore",
        )

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
    mode: AccessMethod,
    settings: object,
) -> LockGrant | None:
    """Acquire the in-flight match lock, or ``None`` when already held.

    Builds the mode-sized TTL (``MATCH_LOCK_BROWSER_TTL_SECONDS`` for
    ``AccessMethod.PLAYWRIGHT_PROXY``, ``MATCH_LOCK_HTTP_TTL_SECONDS``
    otherwise — contracts/match-lock.md "TTL"), generates a fresh fencing
    token (:func:`app_shared.limiter.locks.new_fencing_token`), and calls
    ``await run_in_thread(acquire_match_lock, ...)`` — off-reactor, never
    a synchronous Redis call on the reactor thread. Fail-closed surfaces
    as ``None`` (an acquire Redis error is indistinguishable from "already
    held" to the caller -- both mean "do not fetch", reactor-seam.md).
    """
    ttl_seconds = (
        settings.MATCH_LOCK_BROWSER_TTL_SECONDS
        if mode == AccessMethod.PLAYWRIGHT_PROXY
        else settings.MATCH_LOCK_HTTP_TTL_SECONDS
    )
    key = _match_lock_key(workspace_id, match_id)
    token = _locks.new_fencing_token()
    acquired = await run_in_thread(
        _locks.acquire_match_lock,
        redis,
        key=key,
        token=token,
        ttl_seconds=ttl_seconds,
    )
    if not acquired:
        return None
    return LockGrant(key=key, token=token)


async def release_lock(redis: object, *, key: str, token: str) -> None:
    """Release a previously-acquired match lock (fencing compare-and-delete).

    Off-reactor via ``run_in_thread``. Redis errors are logged and
    swallowed inside ``app_shared.limiter.locks.release_match_lock``
    (TTL reclaims regardless, D3) -- this never raises.
    """
    await run_in_thread(_locks.release_match_lock, redis, key=key, token=token)
