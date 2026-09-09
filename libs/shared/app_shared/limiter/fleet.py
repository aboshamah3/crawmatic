"""Fleet-wide host admission at the physical request boundary (EPA B5, F10).

Every limiter primitive that existed before this module is
**workspace-namespaced** (``rate_key``/``semaphore_key`` carry the
``workspace_id`` immediately after the family prefix, Principle II). That
is correct for fairness between tenants and wrong for the host: an
``amazon.sa`` edge sees one fleet, not N workspaces, so ten tenants each
politely inside their own 90 rpm ceiling still hit the host with 900 rpm
and get the whole fleet blocked. This module is the missing gate — one
token bucket and one concurrency semaphore per ``(domain, transport)``,
shared by the whole fleet, taken at the physical request boundary
*in addition to* (never instead of) the tenant limits.

Design notes:

* **Reuses the existing Lua**, deliberately: ``bucket.acquire_token`` /
  ``bucket.acquire_slot`` / ``bucket.release_slot`` are already atomic,
  server-clock-based, self-expiring and fail-closed (FR-004/005/023). A
  second, near-identical pair of scripts would be a second thing to get
  wrong. The only difference here is the key shape and the absence of a
  workspace segment.
* **Crash recovery is lease expiry.** A holder that dies without
  releasing frees its slot after ``lease_ttl`` seconds, because
  ``acquire_slot``'s Lua purges ``ZREMRANGEBYSCORE key -inf now`` on
  every acquire (SC-004) — no reaper, no heartbeat.
* **Fail-closed.** Any Redis error inside ``acquire_token``/
  ``acquire_slot`` already surfaces as "not granted"; :func:`admit_fleet`
  therefore returns ``None``. A refused lease is a *deferral*, never a
  terminal failure — the caller re-enters the existing backoff/requeue/
  DEFERRED path.
* **Pure Redis + stdlib**, exactly like its siblings: no Scrapy, no
  Twisted, no FastAPI. :func:`resolve_fleet_limits` is the one function
  that touches the database, and it does so only through a
  caller-supplied session factory that it invokes lazily on a cache miss
  — the ORM model is imported inside the function body so importing this
  module never drags in the model layer.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time as _time
from dataclasses import dataclass
from typing import Any, Callable

from app_shared.enums import StrEnum
from app_shared.limiter import bucket as _bucket
from app_shared.limiter.keys import fleet_rate_key, fleet_semaphore_key

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FLEET_LIMITS_CACHE_SECONDS",
    "FleetLease",
    "FleetLimits",
    "FleetSnapshot",
    "FleetTransport",
    "admit_fleet",
    "fleet_snapshot",
    "fleet_transport_for",
    "prime_fleet_limits_cache",
    "release_fleet",
    "reset_fleet_limits_cache",
    "resolve_fleet_limits",
]


class FleetTransport(StrEnum):
    """The COARSE transport class a fleet lease is keyed by.

    Deliberately two members, not the five of ``AccessMethod``: a host
    cannot distinguish ``DIRECT_HTTP`` from ``PROXY_HTTP`` (both are one
    TCP request from its point of view), so giving them separate fleet
    ceilings would let the fleet double its own limit by escalating
    transport — the exact failure this gate exists to prevent. A browser
    navigation is genuinely different (one ``page.goto`` pulls dozens of
    child resources over one lease) and keeps its own ceiling.
    """

    HTTP = "HTTP"
    BROWSER = "BROWSER"


def fleet_transport_for(access_method: Any) -> FleetTransport:
    """Map an ``AccessMethod`` (or its string value) to its fleet class.

    Anything naming a Playwright transport is ``BROWSER``; everything
    else — including an unknown/None value — is ``HTTP``, the
    conservative default (HTTP ceilings are the tighter ones per
    physical request).
    """
    raw = str(getattr(access_method, "value", access_method) or "").upper()
    return FleetTransport.BROWSER if "PLAYWRIGHT" in raw else FleetTransport.HTTP


@dataclass(frozen=True)
class FleetLimits:
    """The resolved fleet ceiling for one domain: ``(concurrency,
    rate_per_minute)``. Both are always ``>= 1`` (a safe floor — a
    zero/negative override must never mean "unlimited")."""

    concurrency: int
    rate_per_minute: int


@dataclass(frozen=True)
class FleetLease:
    """A granted fleet admission lease.

    ``semaphore_key``/``token`` are what :func:`release_fleet` needs;
    ``domain``/``transport``/``lease_ttl`` are carried for logging and
    for the caller that must decide whether a still-held lease is worth
    releasing. Frozen and trivially serializable so a Scrapy ``meta``
    dict can carry its two strings across the fetch and hand them back
    on both the response and the error path.
    """

    semaphore_key: str
    token: str
    domain: str
    transport: FleetTransport
    lease_ttl: int


@dataclass(frozen=True)
class FleetSnapshot:
    """Read-only view of a domain's fleet admission pressure: how many
    leases are live right now versus the ceiling.

    Used for denial **reasons** only (e.g. the cost-authorization
    service's concurrency denial message). Admission itself is
    :func:`admit_fleet`'s Redis lease and nothing else — a snapshot is
    stale the instant it is read, so deciding anything from it would
    reintroduce exactly the check-then-act race the Lua removes.
    """

    domain: str
    transport: FleetTransport
    in_flight: int
    concurrency: int


# --------------------------------------------------------------------------
# 1. Admission
# --------------------------------------------------------------------------


def admit_fleet(
    redis: Any,
    *,
    domain: str,
    transport: Any,
    rate_per_minute: int,
    concurrency: int,
    lease_ttl: int,
) -> FleetLease | None:
    """Take one fleet-wide host lease for ``(domain, transport)``.

    Rate bucket first, then the concurrency slot — the same order and the
    same reasoning as the tenant path in the scraping runtime's
    reactor-safe limiter seam: a bucket denial must never consume a slot,
    and a token spent against a full semaphore is not refunded (the bucket
    self-refills; refunding would open a refund race and could over-grant).

    Returns the :class:`FleetLease` to release later, or ``None`` when
    either gate refuses **or** Redis errored (fail-closed, FR-023). A
    ``None`` is a deferral signal, never a terminal failure.

    ``lease_ttl`` is the crash-recovery window: a holder that never calls
    :func:`release_fleet` frees its slot ``lease_ttl`` seconds later,
    because ``acquire_slot``'s Lua purges expired members on every
    acquire.
    """
    fleet_transport = transport if isinstance(transport, FleetTransport) else fleet_transport_for(transport)
    capacity = max(1, int(rate_per_minute))
    limit = max(1, int(concurrency))
    ttl = max(1, int(lease_ttl))

    rate_result = _bucket.acquire_token(
        redis,
        key=fleet_rate_key(domain, fleet_transport),
        # The bucket refills over a 60s window, so its key must outlive
        # one window plus the lease that may be held across it.
        capacity=capacity,
        ttl_seconds=60 + ttl,
    )
    if not rate_result.granted:
        return None

    sem_key = fleet_semaphore_key(domain, fleet_transport)
    token = secrets.token_hex(16)
    granted = _bucket.acquire_slot(
        redis,
        key=sem_key,
        limit=limit,
        token=token,
        slot_ttl_seconds=ttl,
        # The KEY outlives the longest lease it can hold, so the sorted
        # set is never evicted out from under a live holder.
        key_ttl_seconds=ttl * 2,
    )
    if not granted:
        return None

    return FleetLease(
        semaphore_key=sem_key,
        token=token,
        domain=domain,
        transport=fleet_transport,
        lease_ttl=ttl,
    )


def release_fleet(redis: Any, lease: FleetLease | None) -> None:
    """Release a lease taken by :func:`admit_fleet` (``ZREM``).

    Idempotent, ``None``-tolerant, and never raises: Redis errors are
    logged and swallowed inside ``bucket.release_slot`` because the
    lease's TTL reclaims the slot regardless (D3).
    """
    if lease is None:
        return
    _bucket.release_slot(redis, key=lease.semaphore_key, token=lease.token)


def _server_now(redis: Any) -> float:
    """The REDIS server clock, in epoch seconds.

    The semaphore's member scores are written from ``redis.call('TIME')``
    inside the Lua, never from a worker's wall clock (FR-004), so a
    snapshot that filtered on the *worker's* clock would misreport every
    live lease the moment the two drifted — or, against a test double
    with a controllable clock, misreport all of them. Falls back to the
    local clock only when the client cannot answer.
    """
    try:
        seconds, microseconds = redis.time()
        return float(seconds) + float(microseconds) / 1_000_000
    except Exception:  # noqa: BLE001 - a reasons-only read never raises
        return _time.time()


def fleet_snapshot(
    redis: Any, *, domain: str, transport: Any, concurrency: int
) -> FleetSnapshot:
    """Count the live fleet leases for ``(domain, transport)``.

    Reasons only — see :class:`FleetSnapshot`. Expired members are
    excluded by scoring the ``ZCOUNT`` from *now* forward (the same
    boundary ``acquire_slot``'s Lua purges at), so a crashed holder is
    not counted even before the next acquire sweeps it. Any Redis error
    reports ``in_flight=0`` rather than raising: a denial *message* must
    never be able to break the denial it is explaining.
    """
    fleet_transport = transport if isinstance(transport, FleetTransport) else fleet_transport_for(transport)
    key = fleet_semaphore_key(domain, fleet_transport)
    try:
        in_flight = int(redis.zcount(key, _server_now(redis), "+inf"))
    except Exception:  # noqa: BLE001 - reasons only, never breaks the caller
        logger.warning("app_shared.limiter.fleet: snapshot failed for key=%s", key, exc_info=True)
        in_flight = 0
    return FleetSnapshot(
        domain=domain,
        transport=fleet_transport,
        in_flight=in_flight,
        concurrency=max(1, int(concurrency)),
    )


# --------------------------------------------------------------------------
# 2. Per-domain override resolution (`domain_rules`)
# --------------------------------------------------------------------------

#: Default per-process cache TTL for :func:`resolve_fleet_limits`. Same
#: shape and same 60s generation as ``app_shared.domains.state_lookup
#: .get_domain_state`` — an operator's override is fleet-visible within
#: one generation while costing at most one tiny indexed SELECT per
#: generation per process, instead of one per outbound request.
DEFAULT_FLEET_LIMITS_CACHE_SECONDS = 60


@dataclass(frozen=True)
class _LimitsCacheEntry:
    limits: FleetLimits
    fetched_at: float


_limits_cache: dict[str, _LimitsCacheEntry] = {}
_limits_cache_lock = threading.Lock()


def reset_fleet_limits_cache() -> None:
    """Drop the per-process fleet-limits cache (tests, fork-safety)."""
    global _limits_cache
    with _limits_cache_lock:
        _limits_cache = {}


def resolve_fleet_limits(
    domain: str,
    *,
    settings: Any,
    session_factory: Callable[[], Any] | None = None,
    cache_seconds: int = DEFAULT_FLEET_LIMITS_CACHE_SECONDS,
    monotonic: Any = None,
) -> FleetLimits:
    """Resolve ``domain``'s fleet ceiling: ``domain_rules`` override, else
    the ``FLEET_HOST_*_DEFAULT`` settings.

    ``session_factory`` is a zero-argument callable returning a **context
    manager** yielding a SQLAlchemy ``Session`` (e.g.
    ``lambda: workspace_txn(workspace_id)``). It is invoked ONLY on a
    cache miss, so the hot path costs a dict lookup. Omitting it (or any
    error reaching the database) resolves to the settings defaults and
    logs — the Redis lease is still enforced at the configured default,
    so a database blip degrades the override, never the admission gate.

    **Blocking** (a DB round trip on a cache miss): a caller on the
    Twisted reactor must run it inside that runtime's thread-pool offload
    seam, never on the reactor thread — or, better, warm the cache with
    :func:`prime_fleet_limits_cache` and then call this with no
    ``session_factory`` at all, which never blocks.
    """
    clock = monotonic or _time.monotonic
    nowm = clock()

    defaults = FleetLimits(
        concurrency=max(1, int(getattr(settings, "FLEET_HOST_CONCURRENCY_DEFAULT", 6))),
        rate_per_minute=max(1, int(getattr(settings, "FLEET_HOST_RATE_PER_MINUTE_DEFAULT", 90))),
    )

    cached = _limits_cache.get(domain)
    if cached is not None and (nowm - cached.fetched_at) < cache_seconds:
        return cached.limits

    if session_factory is None:
        return defaults

    try:
        from sqlalchemy import select

        from app_shared.models.domain_rules import DomainRule

        with session_factory() as session:
            row = session.execute(
                select(DomainRule.fleet_concurrency, DomainRule.fleet_rate_per_minute).where(
                    DomainRule.domain == domain
                )
            ).first()
    except Exception:  # noqa: BLE001 - degrade to defaults, never break admission
        logger.warning(
            "app_shared.limiter.fleet: domain_rules lookup failed for domain=%s "
            "-- falling back to fleet defaults",
            domain,
            exc_info=True,
        )
        return defaults

    if row is None:
        limits = defaults
    else:
        concurrency, rate_per_minute = row
        limits = FleetLimits(
            concurrency=(
                max(1, int(concurrency)) if concurrency is not None else defaults.concurrency
            ),
            rate_per_minute=(
                max(1, int(rate_per_minute))
                if rate_per_minute is not None
                else defaults.rate_per_minute
            ),
        )

    with _limits_cache_lock:
        _limits_cache[domain] = _LimitsCacheEntry(limits=limits, fetched_at=nowm)
    return limits


def prime_fleet_limits_cache(
    session: Any,
    domains: Any,
    *,
    settings: Any,
    monotonic: Any = None,
) -> None:
    """Warm :func:`resolve_fleet_limits`' cache for ``domains`` in ONE query.

    Called from the spider's target load, which already holds a session
    inside an off-reactor transaction and already knows every domain the
    run will touch. Warming there is what lets
    :func:`resolve_fleet_limits` be called with no ``session_factory`` on
    the hot path — a pure dict lookup on the Twisted reactor thread, no
    round trip, no thread hop, per outbound request.

    Every requested domain gets an entry, including domains with no
    ``domain_rules`` row (they cache the settings defaults) — so a
    domain nobody has overridden costs zero further queries for a whole
    cache generation instead of one per request. Any error is logged and
    swallowed: a cold cache degrades to the defaults, never to a failed
    scrape.
    """
    wanted = sorted({d for d in domains if d})
    if not wanted:
        return

    clock = monotonic or _time.monotonic
    nowm = clock()
    defaults = FleetLimits(
        concurrency=max(1, int(getattr(settings, "FLEET_HOST_CONCURRENCY_DEFAULT", 6))),
        rate_per_minute=max(1, int(getattr(settings, "FLEET_HOST_RATE_PER_MINUTE_DEFAULT", 90))),
    )

    try:
        from sqlalchemy import select

        from app_shared.models.domain_rules import DomainRule

        rows = session.execute(
            select(
                DomainRule.domain,
                DomainRule.fleet_concurrency,
                DomainRule.fleet_rate_per_minute,
            ).where(DomainRule.domain.in_(wanted))
        ).all()
    except Exception:  # noqa: BLE001 - a cold cache is defaults, never a failure
        logger.warning(
            "app_shared.limiter.fleet: prime_fleet_limits_cache failed for %d domain(s) "
            "-- fleet defaults apply",
            len(wanted),
            exc_info=True,
        )
        return

    overrides = {
        domain: FleetLimits(
            concurrency=(
                max(1, int(concurrency)) if concurrency is not None else defaults.concurrency
            ),
            rate_per_minute=(
                max(1, int(rate_per_minute))
                if rate_per_minute is not None
                else defaults.rate_per_minute
            ),
        )
        for domain, concurrency, rate_per_minute in rows
    }

    with _limits_cache_lock:
        for domain in wanted:
            _limits_cache[domain] = _LimitsCacheEntry(
                limits=overrides.get(domain, defaults), fetched_at=nowm
            )
