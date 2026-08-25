"""Transport-neutral request and result types shared by every adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from app_shared.enums import ExtractionMethod, StockStatus

from scrape_core.extraction.result import ExtractionCandidate

__all__ = [
    "AdapterContext",
    "AdapterOutcome",
    "AdapterRequest",
    "AdapterResponse",
    "AdapterResult",
    "IdentityEvidence",
    "IdentityStatus",
]


class IdentityStatus(StrEnum):
    """The centrally evaluated relationship between target and response."""

    VALID = "VALID"
    UNVERIFIED = "UNVERIFIED"
    NOT_LISTED = "NOT_LISTED"
    MISMATCH = "MISMATCH"
    #: EPA B4: the response is a valid product, but which variant it
    #: refers to could not be established (ambiguous, or the identifiers
    #: name another product). Distinct from ``NOT_LISTED`` (the store
    #: says the product is gone) and from ``MISMATCH`` (we proved we
    #: fetched a different product). Never allows extraction.
    UNRESOLVED = "UNRESOLVED"


class AdapterOutcome(StrEnum):
    """Adapter-level outcomes; strategy resolution maps these to job outcomes."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_LISTED = "NOT_LISTED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    #: EPA B4: identity could not be resolved on an otherwise valid
    #: response. Maps to ``ScrapeErrorCode.IDENTITY_UNRESOLVED`` (target
    #: outcome ``FAILED``), and flags the match ``NEEDS_REVIEW`` in the
    #: A6 ``match_audit_classifications`` sidecar. Never ``NOT_LISTED``.
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    REPAIRED = "REPAIRED"


@dataclass(frozen=True)
class IdentityEvidence:
    status: IdentityStatus
    expected_identifier: str | None = None
    observed_identifier: str | None = None
    source: str | None = None
    reason: str | None = None

    @property
    def allows_extraction(self) -> bool:
        return self.status in (IdentityStatus.VALID, IdentityStatus.UNVERIFIED)


@dataclass(frozen=True)
class AdapterContext:
    """Data resolved by the caller before an adapter request is built.

    Product identity and variant identity are deliberately separate.  A
    Shopify product handle, for example, must not be compared to its variant
    SKU.  ``values`` supports future configuration placeholders without
    coupling this package to an ORM model or a particular customer.
    """

    target_url: str
    identifier: str | None = None
    variant_identifier: str | None = None
    sku: str | None = None
    expected_currency: str | None = None
    profile: Any = None
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_target(
        cls,
        target: Any,
        *,
        identifier: str | None = None,
        expected_currency: str | None = None,
        values: Mapping[str, Any] | None = None,
    ) -> AdapterContext:
        """Build context from either spider's duck-typed target shape."""
        return cls(
            target_url=str(getattr(target, "url")),
            identifier=identifier,
            variant_identifier=getattr(target, "competitor_variant_identifier", None),
            sku=getattr(target, "competitor_variant_sku", None),
            expected_currency=expected_currency,
            profile=getattr(target, "profile", None),
            values=values or {},
        )

    def value(self, source: str) -> Any:
        known = {
            "identifier": self.identifier,
            "competitor_variant_identifier": self.variant_identifier,
            "variant_identifier": self.variant_identifier,
            "competitor_variant_sku": self.sku,
            "sku": self.sku,
            "target_url": self.target_url,
            "product_url": self.target_url,
        }
        return known.get(source, self.values.get(source))


@dataclass(frozen=True)
class AdapterRequest:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = True


@dataclass(frozen=True)
class AdapterResponse:
    body: str | bytes
    final_url: str
    requested_url: str | None = None
    status: int | None = None
    redirect_history: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterResult:
    """Common output for HTML, platform JSON and repair adapters.

    ``candidate`` always crosses the existing ``validate_candidate`` money
    boundary before persistence. ``old_price_text`` intentionally remains raw
    for the same reason; callers must parse it with ``app_shared.money`` only
    after the current-price candidate has been accepted.
    """

    outcome: AdapterOutcome
    final_url: str
    identity: IdentityEvidence
    candidate: ExtractionCandidate | None = None
    old_price_text: str | None = None
    canonical_url: str | None = None
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def extraction_method(self) -> ExtractionMethod | None:
        return self.candidate.method if self.candidate else None

    @property
    def currency(self) -> str | None:
        return self.candidate.currency if self.candidate else None

    @property
    def stock(self) -> StockStatus | None:
        return self.candidate.stock if self.candidate else None
