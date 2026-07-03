"""Product/variant API DTOs (`contracts/api-products.md`, `contracts/api-variants.md`).

Pydantic v2 request/response models for the catalog endpoints
(``apps/api/app/routers/products.py`` / ``routers/variants.py``). Kept in
``apps/api`` (never ``app_shared``) so the framework-agnostic catalog
core never depends on Pydantic (research D7).

Money is always exchanged as a boundary-validated ``Decimal`` (never a
float): :func:`_validate_money` mirrors the same finite / non-NaN /
non-Infinity / ``NUMERIC(18,4)``-scale rules as
``app_shared.money.Money`` so a malformed payload is rejected as a
``422`` at the API boundary rather than surfacing as a DB error.
Currency is a 3-letter code, also boundary-validated (SC-007).

Bulk-upsert note (US2, deferred to a later phase of this feature): a
product that arrives with neither ``external_id`` nor ``sku`` has no
identity for ``ON CONFLICT`` matching, so bulk-upsert always inserts it
fresh (never matched/updated) — callers that want idempotent upsert
semantics must supply at least one of those two fields.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app_shared.enums import ProductStatus, VariantStatus

# Kept in sync with app_shared.money.MONEY_SCALE (NUMERIC(18,4)) — money
# is validated identically at both the API boundary (here) and the DB
# column boundary (app_shared.money.Money), so a malformed value is
# rejected as a 422 before it ever reaches a Money-typed column.
MONEY_SCALE = 4


def _validate_money(value: Any) -> Decimal:
    """Coerce/validate a price-like value to a finite, scale-limited ``Decimal``.

    Rejects ``float``/``bool`` outright (money is never a float), then
    rejects non-finite (``NaN``/``Infinity``) and over-scale (more than
    ``MONEY_SCALE`` fractional digits) values — never silently rounds.
    """
    if isinstance(value, bool):
        raise ValueError("price/current_price must not be a bool")
    if isinstance(value, float):
        raise ValueError(
            "price/current_price must not be a float; pass a string or Decimal"
        )

    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{value!r} is not a valid decimal") from exc
    else:
        raise ValueError(f"unsupported price/current_price type: {type(value)!r}")

    if not decimal_value.is_finite():
        raise ValueError(
            "price/current_price must be finite (NaN/Infinity are rejected)"
        )

    exponent = decimal_value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > MONEY_SCALE:
        raise ValueError(
            f"price/current_price supports at most {MONEY_SCALE} decimal places"
        )
    return decimal_value


def _validate_currency(value: str) -> str:
    """A 3-letter alphabetic currency code, normalized to uppercase."""
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise ValueError("currency must be a 3-letter alphabetic code (e.g. 'USD')")
    return value.upper()


# --- Variant DTOs ------------------------------------------------------------


class VariantCreate(BaseModel):
    """A single explicit variant on a `POST /v1/products` payload."""

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    sku: str | None = None
    barcode: str | None = None
    title: str
    option_values: dict[str, Any] | None = None
    price: Decimal
    currency: str
    url: str | None = None
    status: VariantStatus = VariantStatus.ACTIVE

    @field_validator("price", mode="before")
    @classmethod
    def _check_price(cls, v: Any) -> Decimal:
        return _validate_money(v)

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        return _validate_currency(v)


class VariantResponse(BaseModel):
    """A `product_variants` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    external_id: str | None
    sku: str | None
    barcode: str | None
    title: str
    option_values: dict[str, Any] | None
    current_price: Decimal
    currency: str
    url: str | None
    status: VariantStatus
    created_at: datetime
    updated_at: datetime


class VariantUpdate(BaseModel):
    """`PATCH /v1/variants/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    sku: str | None = None
    barcode: str | None = None
    title: str | None = None
    option_values: dict[str, Any] | None = None
    price: Decimal | None = None
    currency: str | None = None
    url: str | None = None
    status: VariantStatus | None = None

    @field_validator("price", mode="before")
    @classmethod
    def _check_price(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return _validate_money(v)

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_currency(v)


class VariantListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/variants`."""

    items: list[VariantResponse]
    next_cursor: str | None


# --- Product DTOs --------------------------------------------------------


class ProductCreate(BaseModel):
    """`POST /v1/products` request body.

    `price`/`currency` are optional here at the schema level (they only
    seed the auto-derived default variant); the router enforces that a
    product arriving with **no** `variants` MUST supply both (422 per
    `contracts/api-products.md`) — a schema-level requirement would
    incorrectly also demand them on a product that supplies explicit
    `variants` instead.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    sku: str | None = None
    title: str
    brand: str | None = None
    barcode: str | None = None
    url: str | None = None
    status: ProductStatus = ProductStatus.ACTIVE
    price: Decimal | None = None
    currency: str | None = None
    variants: list[VariantCreate] | None = None

    @field_validator("price", mode="before")
    @classmethod
    def _check_price(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return _validate_money(v)

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_currency(v)


class ProductResponse(BaseModel):
    """A `products` row (with its variants) as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str | None
    sku: str | None
    title: str
    brand: str | None
    barcode: str | None
    url: str | None
    status: ProductStatus
    created_at: datetime
    updated_at: datetime
    variants: list[VariantResponse] = []


class ProductUpdate(BaseModel):
    """`PATCH /v1/products/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    sku: str | None = None
    title: str | None = None
    brand: str | None = None
    barcode: str | None = None
    url: str | None = None
    status: ProductStatus | None = None


class ProductListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/products`."""

    items: list[ProductResponse]
    next_cursor: str | None


# --- Shared: delete-outcome ---------------------------------------------


class DeleteOutcome(BaseModel):
    """`DELETE` response body (FR-017): reports which outcome occurred.

    No dependent history exists yet in this spec, so every delete is
    currently `"hard_deleted"`; the shape is structured so a future
    archive-by-status path can return `"archived"` without a response
    schema change.
    """

    id: uuid.UUID
    outcome: Literal["hard_deleted", "archived"]
