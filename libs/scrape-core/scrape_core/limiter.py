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

from scrape_core.db import run_in_thread  # noqa: F401 — used by the T012/T021 wrapper bodies

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

    Stub — filled in by T012 (US1). Must run both checks via
    ``await run_in_thread(...)`` (never a synchronous Redis call on
    the reactor thread) and propagate fail-closed semantics as
    ``granted=False`` rather than raising (reactor-seam.md).
    """
    raise NotImplementedError("acquire_permission is implemented in US1 (T012)")


async def release_slot(redis: object, *, key: str, token: str) -> None:
    """Release a previously-acquired semaphore slot.

    Stub — filled in by T012 (US1).
    """
    raise NotImplementedError("release_slot is implemented in US1 (T012)")


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
