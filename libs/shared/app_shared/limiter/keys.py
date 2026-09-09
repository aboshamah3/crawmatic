"""Workspace-namespaced Redis key builders (contracts/rate-limiter.md,
contracts/match-lock.md; FR-002/003/009/010).

Pure stdlib string formatting — no Redis import, no I/O. Every key
family is prefixed with ``workspace_id`` immediately after the family
prefix so two workspaces on the same domain never share a bucket,
semaphore, or lock (Principle II, US1 AS5).
"""

from __future__ import annotations

import uuid

from app_shared.enums import AccessMethod

__all__ = [
    "fleet_rate_key",
    "fleet_semaphore_key",
    "match_lock_key",
    "rate_key",
    "semaphore_key",
]


def rate_key(workspace_id: uuid.UUID | str, domain: str, access_method: AccessMethod) -> str:
    """Token-bucket key: ``rate:{workspace_id}:{domain}:{ACCESS_METHOD}`` (FR-002)."""
    return f"rate:{workspace_id}:{domain}:{access_method.value}"


def semaphore_key(
    workspace_id: uuid.UUID | str, domain: str, access_method: AccessMethod
) -> str:
    """Concurrency-semaphore key: ``semaphore:{workspace_id}:{domain}:{access_method}`` (FR-003)."""
    return f"semaphore:{workspace_id}:{domain}:{access_method.value}"


def match_lock_key(workspace_id: uuid.UUID | str, match_id: uuid.UUID | str) -> str:
    """Match-lock key: ``lock:scrape:{workspace_id}:{match_id}`` (FR-010)."""
    return f"lock:scrape:{workspace_id}:{match_id}"


# --- EPA B5 (F10): FLEET-wide host admission keys ---------------------------
#
# Deliberately WITHOUT a ``workspace_id`` segment -- that is the entire
# point. Every key family above is workspace-namespaced so two tenants on
# one domain never share a bucket (Principle II); these two are the exact
# opposite: the *host* sees one fleet, not N tenants, so admission at the
# physical request boundary must be shared by every workspace. The
# ``fleet:`` prefix keeps them impossible to confuse with the tenant keys
# in a ``KEYS``/``SCAN`` sweep or a Redis dump.
#
# ``transport`` is the COARSE transport class (``"HTTP"`` / ``"BROWSER"``,
# see ``app_shared.limiter.fleet.FleetTransport``), never a full
# ``AccessMethod``: a host cannot tell DIRECT_HTTP from PROXY_HTTP, so
# splitting the fleet ceiling across those two would let the fleet exceed
# it by simply escalating transport -- precisely the failure this admission
# gate exists to prevent. A browser navigation is genuinely a different
# animal (many child resources ride one navigation), so it keeps its own
# ceiling.


def fleet_rate_key(domain: str, transport: str) -> str:
    """Fleet-wide token-bucket key: ``fleet:rate:{domain}:{TRANSPORT}`` (EPA B5/F10).

    No ``workspace_id`` — one bucket per (domain, transport) for the
    WHOLE fleet.
    """
    return f"fleet:rate:{domain}:{_transport_value(transport)}"


def fleet_semaphore_key(domain: str, transport: str) -> str:
    """Fleet-wide concurrency-semaphore key:
    ``fleet:semaphore:{domain}:{TRANSPORT}`` (EPA B5/F10).

    No ``workspace_id`` — one semaphore per (domain, transport) for the
    WHOLE fleet.
    """
    return f"fleet:semaphore:{domain}:{_transport_value(transport)}"


def _transport_value(transport: str) -> str:
    """Normalize a transport to its uppercase key segment.

    Accepts a bare string or any enum-ish object carrying ``.value`` (so a
    ``FleetTransport`` member and the literal ``"HTTP"`` build the same
    key and can never silently address two different Redis keys).
    """
    raw = getattr(transport, "value", transport)
    return str(raw).upper()
