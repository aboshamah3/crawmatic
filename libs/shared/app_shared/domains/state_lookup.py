"""``get_domain_state`` + the C3 authorization rule table (EPA C2,
READY-004/READY-006 critical path).

The authorization service (C3) must consult enforced domain state before
allowing paid or expensive work against a competitor domain. This module
is the ONLY reader of ``DomainPlaybook.state`` for that purpose:

* :func:`get_domain_state` -- ``(session, domain) -> DomainState``, cached
  per-process for up to ``cache_seconds`` (default 60). Mirrors
  ``app_shared.access.breaker.paid_requests_allowed``'s hot-path cache
  shape (a frozen cache-entry dataclass + module-level dict guarded by a
  ``threading.Lock`` + an injectable ``monotonic`` clock for tests) so a
  state change is visible fleet-wide within one cache generation while
  costing at most one tiny indexed SELECT per generation per process. A
  domain with no ``domain_playbooks`` row resolves to
  ``DomainState.UNKNOWN`` -- the same fail-safe default the column itself
  defaults to.
* :func:`authorization_rules_for_state` -- the deny-by-state matrix C3
  consumes, exactly as specified (nothing beyond it):

  * ``QUARANTINED``/``UNSUPPORTED`` -- deny all paid work.
  * ``UNKNOWN`` -- deny broad crawl; only a tiny DIRECT canary is
    permitted.
  * ``DEGRADED`` -- deny expensive escalation by default.

  The three gates nest (``expensive_escalation_allowed`` implies
  ``broad_crawl_allowed`` implies ``paid_allowed``): each gate inherits
  every denial named for a narrower gate and adds its own. States outside
  this stated matrix (``DIRECT_CANARY``, ``PROFILE_CANARY``, ``ACTIVE``)
  are unrestricted at this layer -- deliberately, since inventing further
  restriction for them is exactly the "rule-table semantics beyond the
  stated matrix" this task's contract calls out as material ambiguity
  rather than something to guess. The full lifecycle machine (W4.1) owns
  any further nuance for those states.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.models.domain_playbooks import DomainPlaybook, DomainState

__all__ = [
    "DEFAULT_CACHE_SECONDS",
    "DomainAuthorizationRules",
    "authorization_rules_for_state",
    "get_domain_state",
    "reset_domain_state_cache",
]

#: Default per-process cache TTL for :func:`get_domain_state` (task
#: contract: "cached ≤ 60s").
DEFAULT_CACHE_SECONDS = 60


# --------------------------------------------------------------------------
# 1. Cached lookup
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _StateCacheEntry:
    state: DomainState
    fetched_at: float


#: Per-process cache, keyed by bare domain. Not per-workspace:
#: ``domain_playbooks`` itself has no workspace column (operator-curated
#: global reference data — see the model's module docstring).
_state_cache: dict[str, _StateCacheEntry] = {}
_state_cache_lock = threading.Lock()


def reset_domain_state_cache() -> None:
    """Drop the per-process domain-state cache (tests, fork-safety)."""
    global _state_cache
    with _state_cache_lock:
        _state_cache = {}


def get_domain_state(
    session: Session,
    domain: str,
    *,
    cache_seconds: int = DEFAULT_CACHE_SECONDS,
    monotonic: Any = None,
) -> DomainState:
    """Return the enforced :class:`DomainState` for ``domain``.

    Cached per-process for up to ``cache_seconds`` (default
    :data:`DEFAULT_CACHE_SECONDS`), keyed by the bare domain string exactly
    as ``domain_playbooks.domain``/``competitors.domain`` store it (no
    scheme, no ``www.``). A domain with no ``domain_playbooks`` row at all
    resolves to ``DomainState.UNKNOWN``, matching the column's own
    default for a domain that *does* have a row but hasn't been
    certified.

    ``monotonic`` is an injectable clock (defaults to
    ``time.monotonic``) purely for deterministic cache-TTL tests —
    mirrors ``access.breaker.paid_requests_allowed``'s parameter of the
    same name.
    """
    clock = monotonic or _time.monotonic
    nowm = clock()

    cached = _state_cache.get(domain)
    if cached is not None and (nowm - cached.fetched_at) < cache_seconds:
        return cached.state

    row = session.execute(
        select(DomainPlaybook.state).where(DomainPlaybook.domain == domain)
    ).first()
    state = row[0] if row is not None else DomainState.UNKNOWN

    with _state_cache_lock:
        _state_cache[domain] = _StateCacheEntry(state=state, fetched_at=nowm)
    return state


# --------------------------------------------------------------------------
# 2. Authorization rule table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainAuthorizationRules:
    """One rule-table row: the minimum authorization contract C3 consults
    for a single :class:`DomainState`.

    Three independent-looking but nested gates, each a superset of the
    next (``paid_allowed`` >= ``broad_crawl_allowed`` >=
    ``expensive_escalation_allowed``) — a state can only get MORE
    restrictive moving down the list, never contradictory (e.g. broad
    crawl permitted while paid work is denied is not representable).
    """

    #: False for ``QUARANTINED``/``UNSUPPORTED`` — deny all paid work.
    paid_allowed: bool
    #: False for ``UNKNOWN`` (and anything ``paid_allowed`` already
    #: denies) — only a tiny DIRECT canary is permitted.
    broad_crawl_allowed: bool
    #: False for ``DEGRADED`` (and anything ``broad_crawl_allowed``
    #: already denies) — expensive escalation is denied by default.
    expensive_escalation_allowed: bool


#: States for which "deny all paid" applies outright.
_PAID_DENIED_STATES = frozenset({DomainState.QUARANTINED, DomainState.UNSUPPORTED})


def authorization_rules_for_state(state: DomainState) -> DomainAuthorizationRules:
    """The rule-table row C3 consults for one :class:`DomainState`.

    Implements exactly the stated matrix:

    * ``QUARANTINED``/``UNSUPPORTED`` -> deny all paid.
    * ``UNKNOWN`` -> deny broad crawl (tiny direct canary only).
    * ``DEGRADED`` -> deny expensive escalation by default.

    Every other state (``DIRECT_CANARY``, ``PROFILE_CANARY``, ``ACTIVE``)
    is fully permitted at this layer.
    """
    paid_allowed = state not in _PAID_DENIED_STATES
    broad_crawl_allowed = paid_allowed and state is not DomainState.UNKNOWN
    expensive_escalation_allowed = broad_crawl_allowed and state is not DomainState.DEGRADED
    return DomainAuthorizationRules(
        paid_allowed=paid_allowed,
        broad_crawl_allowed=broad_crawl_allowed,
        expensive_escalation_allowed=expensive_escalation_allowed,
    )
