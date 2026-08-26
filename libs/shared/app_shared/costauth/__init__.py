"""Unified cost authorization + reservation (EPA C3, READY-006).

Public surface: :mod:`app_shared.costauth.service`. Nothing else in this
package is importable API — the service is deliberately the ONLY door to
the reservation ledger, because a second writer of ``cost_reservations``
is a second answer to "how much is this workspace allowed to spend".
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
    DenialReason,
    SettledCost,
    authorize_or_none,
    estimate_bytes,
    estimate_cost_minor_units,
    period_key_for,
    release_reservations_for_scrape_job,
    sweep_expired_reservations,
)
# Re-exported from the model module so a call site needs ONE import to
# build a request: the purpose vocabulary is part of the authorization
# contract, not an implementation detail of the schema.
from app_shared.models.cost_authorization import AuthorizationPurpose, ReservationState

__all__ = [
    "AuthorizationPurpose",
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
    "SettledCost",
    "authorize_or_none",
    "estimate_bytes",
    "estimate_cost_minor_units",
    "period_key_for",
    "release_reservations_for_scrape_job",
    "sweep_expired_reservations",
]
