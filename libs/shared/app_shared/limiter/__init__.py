"""Distributed rate-limiting & in-flight match-lock primitives (SPEC-11).

Pure Redis logic — stdlib + an injected ``redis.Redis``-shaped client
only, **no** Scrapy/Twisted/FastAPI import — the direct sibling of
``app_shared.access.budget`` (contracts/rate-limiter.md,
contracts/match-lock.md). The reactor-safe orchestration that wraps
these functions off the Twisted reactor lives in a separate scraping-
runtime library, owned entirely by Constitution V — this package must
never import it, or even name it by string, anywhere under this
directory (see ``tests/unit/test_import_boundaries.py``).

Public API (T034), re-exported from the four submodules so a caller
needs only ``from app_shared.limiter import ...``:

* ``keys`` — workspace-namespaced Redis key builders (``rate_key``,
  ``semaphore_key``, ``match_lock_key``).
* ``limits`` — effective per-domain limit resolution (``EffectiveLimits``,
  ``resolve_limits``).
* ``bucket`` — the atomic token-bucket + concurrency-semaphore primitives
  (``AcquireResult``, ``acquire_token``, ``acquire_slot``,
  ``release_slot``).
* ``fleet`` — EPA B5/F10's FLEET-wide (workspace-independent) host
  admission gate at the physical request boundary (``FleetLease``,
  ``admit_fleet``, ``release_fleet``, ``resolve_fleet_limits``), built
  on the same ``bucket`` Lua as the tenant primitives above.
* ``locks`` — the fencing-token match lock (``new_fencing_token``,
  ``acquire_match_lock``, ``release_match_lock``).
"""

from __future__ import annotations

from app_shared.limiter.bucket import AcquireResult, acquire_slot, acquire_token, release_slot
from app_shared.limiter.fleet import (
    FleetLease,
    FleetLimits,
    FleetSnapshot,
    FleetTransport,
    admit_fleet,
    fleet_snapshot,
    fleet_transport_for,
    release_fleet,
    resolve_fleet_limits,
)
from app_shared.limiter.keys import (
    fleet_rate_key,
    fleet_semaphore_key,
    match_lock_key,
    rate_key,
    semaphore_key,
)
from app_shared.limiter.limits import EffectiveLimits, resolve_limits
from app_shared.limiter.locks import acquire_match_lock, new_fencing_token, release_match_lock

__all__ = [
    "AcquireResult",
    "EffectiveLimits",
    "FleetLease",
    "FleetLimits",
    "FleetSnapshot",
    "FleetTransport",
    "acquire_match_lock",
    "admit_fleet",
    "acquire_slot",
    "acquire_token",
    "fleet_rate_key",
    "fleet_semaphore_key",
    "fleet_snapshot",
    "fleet_transport_for",
    "match_lock_key",
    "new_fencing_token",
    "rate_key",
    "release_fleet",
    "release_match_lock",
    "release_slot",
    "resolve_fleet_limits",
    "resolve_limits",
    "semaphore_key",
]
