"""``OfferObservation`` — the canonical, versioned offer-observation contract.

Per the W3.1 plan (READY-012, report §7): a superset of the extraction
fields ``app_shared.models.observations.PriceObservation`` has carried
since SPEC-07 — one observation id, source/canonical URL, merchant
domain and market, an observed timestamp with its source timezone,
expected-vs-observed product identity, seller/marketplace facts,
pricing (currency, item/list/shipping/fees/deposit/unit price, a landed
total, tax-inclusion), promotion facts, extraction/access provenance,
confidence **broken out per dimension** (never one blended score), and
freshness/comparability/review classification.

``schema_version`` is a fixed ``Literal["1"]`` — not a free string — so
a payload from a future incompatible schema fails validation instead of
being silently misread, and covers the field-set/representation as a
single versioned unit (never bump it for a field that changes meaning
without changing the literal).

Money
-----
Every monetary field is validated through
:func:`app_shared.money.parse_money` — the engine's existing, single
Money contract (``app_shared.money.Money``, an exact ``Decimal`` over
``NUMERIC(18,4)``, never a float). See the module-level ``_MONEY_FIELDS``
comment below for why this contract was chosen over a scaled-integer
minor-units encoding, despite the W3.1 plan text describing the latter.

Unknown vs. zero
----------------
Every optional field defaults to ``None`` and nothing in this module
ever substitutes ``0``/``0.0``/``""`` for an absent fact — an omitted
price is *unknown*, not free; an omitted confidence dimension is
*unmeasured*, not zero confidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app_shared.enums import AccessMethod, StockStatus
from app_shared.money import parse_money

#: Fields validated through the engine's existing Money contract
#: (``app_shared.money.parse_money`` — the pure boundary
#: ``app_shared.money.Money`` itself delegates to). The W3.1 plan text
#: describes "integer minor units with currency exponent" — that is
#: `app_shared.models.network_operations`'s ledger convention (a
#: **different**, newer money representation introduced by the
#: concurrent EPA C1 task for provider-cost accounting), not what
#: ``app_shared.money.Money`` actually implements. Reusing the plan's
#: named file (``money.py``) rather than its paraphrase keeps this
#: contract consistent with the columns it is persisted alongside —
#: ``PriceObservation.price``/``old_price`` and
#: ``MatchCurrentPrice.price``/``old_price`` are already
#: ``Money()`` (``NUMERIC(18,4)``, exact ``Decimal``) — instead of
#: introducing a THIRD, incompatible money encoding on the same table.
#: Recorded as a deviation in the W3.1 task report.
_MONEY_FIELD_NAMES: tuple[str, ...] = (
    "item_price",
    "list_price",
    "shipping_cost",
    "fees",
    "deposit",
    "unit_price",
    "landed_total",
)

#: Confidence dimensions live in [0, 1] — a fraction, never a float in
#: the sense §19 forbids for money, but still validated as an exact
#: ``Decimal`` for the same "no silent binary-fraction drift" reason,
#: and to stay serialization-symmetric with the money fields above.
_CONFIDENCE_FIELD_NAMES: tuple[str, ...] = (
    "discovery",
    "identity",
    "extraction",
    "comparability",
)

#: Landed total is computed, never taken from caller input: it is the
#: sum of exactly these components, and ONLY when every one of them is
#: known (§7: "landed total computed only when ALL components known").
_LANDED_TOTAL_COMPONENTS: tuple[str, ...] = ("item_price", "shipping_cost", "fees", "deposit")


def _validate_money(value: Any) -> Decimal | None:
    """``None`` passes through; everything else goes through §19's
    ``parse_money`` boundary (rejects float/bool/NaN/Infinity/over-scale).

    ``parse_money`` raises ``TypeError`` for a float/bool/wrong-type
    input — but pydantic v2's validators only convert ``ValueError``/
    ``AssertionError`` into a ``ValidationError``; a bare ``TypeError``
    propagates raw and crashes validation instead of failing it. Re-raise
    as ``ValueError`` (same message) so the float/bool rejection surfaces
    as a normal, catchable validation error.
    """
    if value is None:
        return None
    try:
        return parse_money(value)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


def _validate_unit_interval(value: Any) -> Decimal | None:
    """Confidence dimensions: ``None`` passes through; otherwise an exact,
    in-range ``[0, 1]`` ``Decimal`` — same float/bool rejection as money,
    via ``parse_money`` (``non_negative`` doesn't reject the upper bound,
    so that is checked here)."""
    if value is None:
        return None
    try:
        decimal_value = parse_money(value, non_negative=True)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    if decimal_value > 1:
        raise ValueError(
            f"confidence dimensions are a fraction in [0, 1], got {decimal_value!r}"
        )
    return decimal_value


def _validate_aware_datetime(value: Any) -> Any:
    """Reject naive datetimes the same way ``app_shared.models.base.TZDateTime``
    does for persisted columns — a contract meant to be stored alongside
    ``PriceObservation`` should never accept what that column would
    refuse."""
    if isinstance(value, datetime) and value.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed; pass a timezone-aware datetime "
            "(e.g. datetime.now(timezone.utc))"
        )
    return value


class IdentityFacet(BaseModel):
    """One side (expected or observed) of product-identity comparison.

    Every field is optional and independently ``None`` when unknown —
    a partially-identified offer (e.g. a title but no GTIN) is
    representable without inventing placeholder values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sku: str | None = None
    mpn: str | None = None
    gtin: str | None = None
    brand: str | None = None
    model: str | None = None
    title: str | None = None
    #: Free-form variant attributes (e.g. ``{"color": "black", "size": "M"}``).
    variant_attrs: dict[str, str] | None = None
    pack_size: str | None = None
    quantity_unit: str | None = None


class PromotionFacts(BaseModel):
    """A single promotion's evidence, per §7's promotion-facts field set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    coupon_code: str | None = None
    coupon_requirement: str | None = None
    automatic_discount: Decimal | None = None
    member_price: Decimal | None = None
    card_price: Decimal | None = None
    app_price: Decimal | None = None
    bundle_condition: str | None = None
    quantity_condition: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    #: Free-text pointer to the evidence this promotion's facts were read
    #: from (a selector, a raw snippet, or a ``raw_evidence_hash``-style
    #: reference) — kept distinct from the observation's own
    #: ``raw_evidence_hash`` because a promotion can be sourced from a
    #: different fragment of the page than the price itself.
    evidence: str | None = None

    @field_validator(
        "automatic_discount", "member_price", "card_price", "app_price", mode="before"
    )
    @classmethod
    def _validate_promotion_money(cls, value: Any) -> Decimal | None:
        return _validate_money(value)

    @field_validator("start_at", "end_at", mode="before")
    @classmethod
    def _validate_promotion_datetimes(cls, value: Any) -> Any:
        return _validate_aware_datetime(value)


class ConfidenceDimensions(BaseModel):
    """Confidence broken out **separately** per §7 — discovery, identity,
    extraction, and comparability are distinct judgments about distinct
    failure modes, and blending them into one score would hide which one
    a given observation actually lacks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    discovery: Decimal | None = None
    identity: Decimal | None = None
    extraction: Decimal | None = None
    comparability: Decimal | None = None

    @field_validator(*_CONFIDENCE_FIELD_NAMES, mode="before")
    @classmethod
    def _validate_dimension(cls, value: Any) -> Decimal | None:
        return _validate_unit_interval(value)


class OfferObservation(BaseModel):
    """The canonical offer-observation contract (schema_version ``"1"``).

    Persisted alongside ``PriceObservation`` (same row, ``offer_*``
    columns — see ``app_shared.models.observations``): this pydantic
    model is the validation/serialization boundary a scraper's
    extraction result must pass through before being written, and the
    shape read back out for anything that consumes an observation
    (matching, alerting, the SaaS promotion UI).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"

    # --- identity of the observation itself --------------------------
    observation_id: uuid.UUID
    source_url: str
    canonical_url: str | None = None
    domain: str
    market: str | None = None
    observed_at: datetime
    source_timezone: str | None = None

    # --- expected vs. observed product identity -----------------------
    expected_identity: IdentityFacet | None = None
    observed_identity: IdentityFacet | None = None

    # --- seller / marketplace ------------------------------------------
    seller_name: str | None = None
    seller_type: str | None = None
    fulfillment: str | None = None
    stock_status: StockStatus | None = None
    condition: str | None = None

    # --- pricing ---------------------------------------------------------
    currency: str | None = None
    item_price: Decimal | None = None
    list_price: Decimal | None = None
    shipping_cost: Decimal | None = None
    tax_included: bool | None = None
    fees: Decimal | None = None
    deposit: Decimal | None = None
    unit_price: Decimal | None = None
    #: NEVER set by the caller — computed by
    #: :meth:`_compute_landed_total` from ``_LANDED_TOTAL_COMPONENTS``,
    #: and only when every one of them is known.
    landed_total: Decimal | None = None

    # --- promotion facts ---------------------------------------------------
    promotion: PromotionFacts | None = None

    # --- access / extraction provenance -------------------------------
    access_method: AccessMethod | None = None
    profile_version: str | None = None
    parser_version: str | None = None
    #: Content hash of the raw evidence this observation was extracted
    #: from, resolvable via ``app_shared.observations.evidence_store``.
    raw_evidence_hash: str | None = None
    strategy: str | None = None

    # --- confidence (kept separate per dimension) + validation --------
    confidence: ConfidenceDimensions | None = None
    validation_reasons: list[str] | None = None

    # --- freshness / classification --------------------------------------
    expires_at: datetime | None = None
    comparability_class: str | None = None
    rejection_reason: str | None = None
    human_review_required: bool | None = None

    # --- validators ---------------------------------------------------------

    @field_validator(*_MONEY_FIELD_NAMES, mode="before")
    @classmethod
    def _validate_money_fields(cls, value: Any) -> Decimal | None:
        return _validate_money(value)

    @field_validator("observed_at", "expires_at", mode="before")
    @classmethod
    def _validate_datetimes(cls, value: Any) -> Any:
        return _validate_aware_datetime(value)

    @field_validator("currency", mode="after")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        if value is not None and not (len(value) == 3 and value.isalpha() and value.isupper()):
            raise ValueError(f"currency must be an ISO-4217 3-letter uppercase code: {value!r}")
        return value

    @model_validator(mode="after")
    def _compute_landed_total(self) -> "OfferObservation":
        """§7 / TDD step 1: the landed total is computed, never accepted
        as input, and ONLY when every component in
        ``_LANDED_TOTAL_COMPONENTS`` is known — a partially-known set of
        components leaves ``landed_total`` ``None`` rather than silently
        summing what happens to be present (which would understate the
        true total, not represent it)."""
        components = [getattr(self, name) for name in _LANDED_TOTAL_COMPONENTS]
        if all(component is not None for component in components):
            total = sum(components, start=Decimal("0"))
            object.__setattr__(self, "landed_total", parse_money(total))
        else:
            object.__setattr__(self, "landed_total", None)
        return self
