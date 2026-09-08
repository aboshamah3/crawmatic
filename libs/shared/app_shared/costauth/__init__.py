"""Unified cost authorization + reservation (EPA C3, READY-006).

Public surface: :mod:`app_shared.costauth.service` — re-exported below —
plus :mod:`app_shared.costauth.entitlements`, imported by its own path.
The service remains deliberately the ONLY door to the reservation
ledger, because a second writer of ``cost_reservations`` is a second
answer to "how much is this workspace allowed to spend".

``entitlements`` is not a second such door and does not weaken that
rule: it writes ``workspace_entitlements`` — the *evidence* the gate
reads — and never touches a reservation, a budget counter or a limit. It
is kept out of this namespace on purpose, so that "import from
``app_shared.costauth``" keeps meaning "use the gate" and a call site
that seeds or refreshes evidence has to say so in its import line.
"""

from __future__ import annotations

from app_shared.costauth.service import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_DIRECT,
    FLEET_PROVIDER_PROXY,
    AuthorizationGrant,
    AuthorizationRequest,
    CostAuthorizationDenied,
    CostAuthorizationService,
    CrossWorkspaceCoalescingUnsupported,
    ESTIMATED_BROWSER_WALL_SECONDS_PER_REQUEST,
    DenialReason,
    FirstRungReservation,
    SettledCost,
    authorize_or_none,
    costauth_denials_by_reason,
    escalation_reservation,
    estimate_bytes,
    first_rung_reservation,
    period_key_for,
    release_reservations_for_scrape_job,
    reservation_rung,
    reset_costauth_denials,
    sweep_expired_reservations,
)
# Pricing is its own module, and it is the ONLY place a rate lives
# (H4/B2). Re-exported here because every call site that authorizes
# or settles already imports from this package, and a second import
# line is a second chance to reach past the gate for a number.
from app_shared.costauth.pricing import (
    BROWSER_BILLING_RATE_PER_CPU_SECOND,
    BROWSER_CPU_PER_WALL_SECOND,
    PROXY_BILLING_RATE_PER_GIB,
    estimate_reservation_micro_units,
    price_operation_micro_units,
)
# Re-exported from the model module so a call site needs ONE import to
# build a request: the purpose vocabulary is part of the authorization
# contract, not an implementation detail of the schema.
from app_shared.models.cost_authorization import AuthorizationPurpose, ReservationState

__all__ = [
    "AuthorizationPurpose",
    "BROWSER_BILLING_RATE_PER_CPU_SECOND",
    "BROWSER_CPU_PER_WALL_SECOND",
    "PROXY_BILLING_RATE_PER_GIB",
    "ReservationState",
    "FLEET_PROVIDER_BROWSER",
    "FLEET_PROVIDER_DIRECT",
    "FLEET_PROVIDER_PROXY",
    "AuthorizationGrant",
    "AuthorizationRequest",
    "CostAuthorizationDenied",
    "CostAuthorizationService",
    "CrossWorkspaceCoalescingUnsupported",
    "DenialReason",
    "ESTIMATED_BROWSER_WALL_SECONDS_PER_REQUEST",
    "FirstRungReservation",
    "SettledCost",
    "authorize_or_none",
    "costauth_denials_by_reason",
    "escalation_reservation",
    "estimate_bytes",
    "estimate_reservation_micro_units",
    "first_rung_reservation",
    "period_key_for",
    "price_operation_micro_units",
    "release_reservations_for_scrape_job",
    "reservation_rung",
    "reset_costauth_denials",
    "sweep_expired_reservations",
]
