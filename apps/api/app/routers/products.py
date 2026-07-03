"""Products endpoints (`contracts/api-products.md`) — SPEC-04 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. All reads/writes go
through `app_shared.repository.scoped_select`/`scoped_get` — RLS backs
them as the second isolation layer (FR-016, FR-002).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete

from app_shared.catalog.default_variant import derive_default_variant, ensure_at_least_one
from app_shared.models.catalog import Product, ProductVariant
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.catalog import (
    DeleteOutcome,
    ProductCreate,
    ProductListResponse,
    ProductResponse,
    ProductUpdate,
    VariantResponse,
)

router = APIRouter(prefix="/v1/products", tags=["products"])


def _variants_by_product_id(
    session, workspace_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProductVariant]]:
    """Bulk-fetch every variant for `product_ids`, grouped — avoids an N+1 query."""
    if not product_ids:
        return {}
    rows = (
        session.execute(
            scoped_select(ProductVariant, workspace_id).where(
                ProductVariant.product_id.in_(product_ids)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ProductVariant]] = {}
    for row in rows:
        grouped.setdefault(row.product_id, []).append(row)
    return grouped


def _to_product_response(product: Product, variants: list[ProductVariant]) -> ProductResponse:
    return ProductResponse(
        id=product.id,
        external_id=product.external_id,
        sku=product.sku,
        title=product.title,
        brand=product.brand,
        barcode=product.barcode,
        url=product.url,
        status=product.status,
        created_at=product.created_at,
        updated_at=product.updated_at,
        variants=[VariantResponse.model_validate(v) for v in variants],
    )


@router.post("", response_model=ProductResponse, status_code=201)
def create_product(
    payload: ProductCreate,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> ProductResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    explicit_variants = payload.variants or []
    if not explicit_variants and (payload.price is None or payload.currency is None):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "MISSING_PRICE",
                    "message": (
                        "A product created with no explicit variants must supply "
                        "price and currency to seed its default variant."
                    ),
                }
            },
        )

    product = Product(
        workspace_id=principal.workspace_id,
        external_id=payload.external_id,
        sku=payload.sku,
        title=payload.title,
        brand=payload.brand,
        barcode=payload.barcode,
        url=payload.url,
        status=payload.status,
    )
    session.add(product)
    session.flush()  # assigns product.id / created_at

    if explicit_variants:
        variant_dicts = [v.model_dump() for v in explicit_variants]
    else:
        variant_dicts = ensure_at_least_one(
            {"title": payload.title, "sku": payload.sku, "url": payload.url,
             "price": payload.price, "currency": payload.currency},
            [],
        )

    variant_rows: list[ProductVariant] = []
    for v in variant_dicts:
        # Explicit variants (VariantCreate.model_dump()) carry a "price"
        # key; a derived default variant (ensure_at_least_one, which
        # follows the ORM column name) carries "current_price" — accept
        # either so both sources feed the same insert path.
        price = v.get("current_price", v.get("price"))
        variant_rows.append(
            ProductVariant(
                workspace_id=principal.workspace_id,
                product_id=product.id,
                external_id=v.get("external_id"),
                sku=v.get("sku"),
                barcode=v.get("barcode"),
                title=v["title"],
                option_values=v.get("option_values"),
                current_price=price,
                currency=v["currency"],
                url=v.get("url"),
                status=v.get("status") or "active",
            )
        )
    session.add_all(variant_rows)
    session.flush()

    return _to_product_response(product, variant_rows)


@router.get("", response_model=ProductListResponse)
def list_products(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("products:read")),
) -> ProductListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(Product, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(Product, after))
    stmt = stmt.order_by(Product.created_at, Product.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    products: list[Product] = envelope["items"]

    grouped = _variants_by_product_id(
        session, principal.workspace_id, [p.id for p in products]
    )
    items = [_to_product_response(p, grouped.get(p.id, [])) for p in products]
    return ProductListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("products:read")),
) -> ProductResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    product = scoped_get(session, Product, product_id, principal.workspace_id)
    if product is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Product not found."}},
        )

    grouped = _variants_by_product_id(session, principal.workspace_id, [product.id])
    return _to_product_response(product, grouped.get(product.id, []))


@router.patch("/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> ProductResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    product = scoped_get(session, Product, product_id, principal.workspace_id)
    if product is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Product not found."}},
        )

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(product, field, value)
    session.flush()

    grouped = _variants_by_product_id(session, principal.workspace_id, [product.id])
    return _to_product_response(product, grouped.get(product.id, []))


@router.delete("/{product_id}", response_model=DeleteOutcome)
def delete_product(
    product_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    product = scoped_get(session, Product, product_id, principal.workspace_id)
    if product is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Product not found."}},
        )

    # No dependent history exists in this spec -> hard delete (FR-017).
    # Structured so a future archive-by-status path only needs to swap
    # the branch below for `product.status = ProductStatus.ARCHIVED`.
    # Children must go first (FK products <- product_variants).
    session.execute(
        delete(ProductVariant).where(
            ProductVariant.workspace_id == principal.workspace_id,
            ProductVariant.product_id == product.id,
        )
    )
    session.delete(product)
    session.flush()

    return DeleteOutcome(id=product_id, outcome="hard_deleted")
