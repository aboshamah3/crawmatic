"""Request/response models for the catalog index routes
(`app.routers.catalog_index`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class IndexProductQuery(BaseModel):
    """One product to look up. `ref` is the caller's own id, echoed back."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=1000)
    brand: str | None = Field(default=None, max_length=200)
    sku: str | None = Field(default=None, max_length=200)
    mpn: str | None = Field(default=None, max_length=200)
    gtin: str | None = Field(default=None, max_length=200)


class IndexLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[IndexProductQuery] = Field(min_length=1, max_length=50)
    #: ISO currency the candidates must be priced in; "" disables the filter.
    currency: str = Field(default="SAR", max_length=8)
    #: Domains never returned: plain hosts, or `salla.sa/<store>` path stores.
    exclude_domains: list[str] = Field(default_factory=list, max_length=100)
    limit_per_product: int = Field(default=5, ge=1, le=20)
    #: Model and title stages read multi-brand (`pool`) stores only.
    pool_only: bool = True


class IndexCandidateOut(BaseModel):
    domain: str
    host: str
    url: str
    title: str | None
    brand: str | None
    price: Decimal | None
    currency: str | None
    available: bool | None
    #: When the price was observed by the crawl, never "now".
    observed_at: datetime
    store_verdict: str
    #: `barcode`, `model` or `title`.
    stage: str
    score: float
    matched_key: str | None


class IndexLookupResult(BaseModel):
    ref: str
    candidates: list[IndexCandidateOut]


class IndexLookupResponse(BaseModel):
    #: None when no index generation is active (nothing loaded yet).
    generation: int | None
    results: list[IndexLookupResult]


class IndexStatusResponse(BaseModel):
    generation: int | None
    products: int | None
    codes: int | None
    activated_at: datetime | None
    source_built_at: datetime | None


class WorkspaceCandidateOut(IndexCandidateOut):
    product_id: uuid.UUID
    product_variant_id: uuid.UUID


class WorkspaceCandidatesResponse(BaseModel):
    generation: int | None
    candidates: list[WorkspaceCandidateOut]
    #: Pass back as `cursor` to continue; None when the catalogue is done.
    next_cursor: uuid.UUID | None
    variants_scanned: int


class AcceptCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_variant_id: uuid.UUID
    url: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=1000)


class AcceptCandidatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[AcceptCandidate] = Field(min_length=1, max_length=200)


class RejectedCandidate(BaseModel):
    index: int
    #: UNSAFE_URL, UNRESOLVED_VARIANT, DOMAIN_LIMIT_REACHED,
    #: COMPETITOR_ARCHIVED or PROTECTED_LINK_CAP_REACHED.
    code: str
    url: str | None = None


class AcceptCandidatesResponse(BaseModel):
    #: Matches newly created as PAUSED. An existing match is never touched.
    created: int
    competitors_created: int
    rejected: list[RejectedCandidate]
