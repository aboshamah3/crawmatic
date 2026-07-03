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

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app_shared.catalog.consistency import ExactlyOneOfViolation, exactly_one_of
from app_shared.enums import GroupStatus, ProductStatus, VariantStatus

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


# --- Bulk-upsert DTOs (US2, `contracts/catalog-bulk-upsert.md`) --------------
#
# Bulk-upsert note (FR-011): a **product** supplied with neither
# `external_id` nor `sku` has no stable identity key for `ON CONFLICT`
# matching, so bulk-upsert always inserts it fresh on every push (never
# matched/updated, and re-pushing an unmodified identity-less product
# creates a duplicate row rather than being a no-op) -- callers that
# want idempotent upsert semantics must supply at least one of those two
# fields on every product they intend to keep in sync.


class VariantBulkUpsertItem(BaseModel):
    """One variant row nested under a product in `POST /v1/products/bulk-upsert`.

    Identity resolution order (FR-011): `external_id` -> `sku` ->
    `(product_id, title)` (the parent product_id is filled in by the
    router once the parent product's identity resolves).
    """

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


class ProductBulkUpsertItem(BaseModel):
    """One product row in `POST /v1/products/bulk-upsert` -- a Woo/Salla-style
    nested payload where each product optionally carries its own `variants`.

    `price`/`currency` seed the auto-derived default variant exactly like
    `ProductCreate`, only when `variants` is empty/absent; a product
    arriving with **zero** variants and no `price`/`currency` cannot be
    upserted (422) -- see `apps/api/app/routers/products.py`.
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
    variants: list[VariantBulkUpsertItem] | None = None

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


class ProductBulkUpsertRequest(BaseModel):
    """`POST /v1/products/bulk-upsert` request body."""

    model_config = ConfigDict(extra="forbid")

    products: list[ProductBulkUpsertItem]


class ProductBulkUpsertResult(BaseModel):
    """`POST /v1/products/bulk-upsert` response -- every upserted product
    (each carrying >=1 variant, FR-012 tail)."""

    upserted: int
    products: list[ProductResponse]


class VariantBulkUpsertItemStandalone(BaseModel):
    """One row in the standalone `POST /v1/variants/bulk-upsert` payload.

    Each row names its parent product explicitly via exactly one of
    `product_id`, `product_external_id`, or `product_sku` -- resolved
    set-based (one scoped lookup, never a per-row query) and
    workspace-consistency pre-checked (`app_shared.catalog.consistency`):
    a cross-workspace or unresolvable parent reference is rejected
    (422), never silently dropped or left to a raw FK-violation 500.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID | None = None
    product_external_id: str | None = None
    product_sku: str | None = None
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


class VariantsBulkUpsertRequest(BaseModel):
    """`POST /v1/variants/bulk-upsert` request body."""

    model_config = ConfigDict(extra="forbid")

    variants: list[VariantBulkUpsertItemStandalone]


class VariantBulkUpsertResult(BaseModel):
    """`POST /v1/variants/bulk-upsert` response."""

    upserted: int
    variants: list[VariantResponse]


# --- Group DTOs (US3, `contracts/api-product-groups.md`) --------------------


class GroupCreate(BaseModel):
    """`POST /v1/product-groups` request body. ``unique(workspace_id, name)``
    duplicate -> `409` (enforced by the router, not here)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    status: GroupStatus = GroupStatus.ACTIVE


class GroupUpdate(BaseModel):
    """`PATCH /v1/product-groups/{id}` — every field optional (partial update)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    status: GroupStatus | None = None


class GroupItemResponse(BaseModel):
    """A `product_group_items` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_group_id: uuid.UUID
    product_id: uuid.UUID | None
    product_variant_id: uuid.UUID | None
    created_at: datetime


class GroupResponse(BaseModel):
    """A `product_groups` row (with its member items) as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: GroupStatus
    created_at: datetime
    updated_at: datetime
    items: list[GroupItemResponse] = []


class GroupListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/product-groups`."""

    items: list[GroupResponse]
    next_cursor: str | None


class GroupItemCreate(BaseModel):
    """`POST /v1/product-groups/{id}/items` request body.

    Exactly one of `product_id` / `product_variant_id` must be set — the
    "exactly one" rule is the pure `app_shared.catalog.consistency.exactly_one_of`
    core, reused here (not re-implemented) so the API boundary and any
    future non-HTTP caller share one rule (`contracts/workspace-consistency.md`
    Layer 2). The referenced entity must additionally resolve inside the
    caller's workspace — that check runs in the router (composite-FK-backed,
    422/404 on cross-workspace/nonexistent), since it requires a DB lookup
    this pure schema validator cannot perform.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID | None = None
    product_variant_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_exactly_one(self) -> "GroupItemCreate":
        try:
            exactly_one_of(self.product_id, self.product_variant_id)
        except ExactlyOneOfViolation as exc:
            raise ValueError(str(exc)) from exc
        return self


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
