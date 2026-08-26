"""Canonical observation contracts (W3.1, READY-012).

Home for schemas that describe what was observed on a competitor's page
at the offer level — a strictly richer, versioned superset of the
extraction fields ``app_shared.models.observations.PriceObservation``
has carried since SPEC-07. See :mod:`app_shared.observations.offer_observation`
for the contract itself and :mod:`app_shared.observations.evidence_store`
for the governed evidence store backing its ``raw_evidence_hash``.
"""

from __future__ import annotations

from app_shared.observations.offer_observation import (
    ConfidenceDimensions,
    IdentityFacet,
    OfferObservation,
    PromotionFacts,
)

__all__ = [
    "OfferObservation",
    "IdentityFacet",
    "PromotionFacts",
    "ConfidenceDimensions",
]
