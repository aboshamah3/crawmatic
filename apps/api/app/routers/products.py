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
from app_shared.catalog.upsert import (
    build_products_upsert,
    dedup_last_wins,
    plan_upsert,
    resolve_identity,
)
from app_shared.models.catalog import Product, ProductVariant
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.catalog import (
    DeleteOutcome,
    ProductBulkUpsertRequest,
    ProductBulkUpsertResult,
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


@router.post("/bulk-upsert", response_model=ProductBulkUpsertResult, status_code=200)
def bulk_upsert_products(
    payload: ProductBulkUpsertRequest,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> ProductBulkUpsertResult:
    """Set-based bulk upsert (`contracts/catalog-bulk-upsert.md`, FR-010/011/012).

    Woo/Salla-style nested payload: dedup last-wins -> resolve identity
    (`external_id` -> `sku`) -> a bounded number of
    `ON CONFLICT ... DO UPDATE` statements via `app_shared.catalog.upsert`
    (never one statement per product) -> default-variant injection for
    any product arriving with zero explicit variants -> a bounded number
    of variant upsert statements. Runs entirely inside the request's
    workspace-scoped session/transaction (FR-016).
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if not payload.products:
        return ProductBulkUpsertResult(upserted=0, products=[])

    for item in payload.products:
        if not item.variants and (item.price is None or item.currency is None):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "MISSING_PRICE",
                        "message": (
                            "A bulk-upsert product arriving with no explicit "
                            "variants must supply price and currency to seed "
                            "its default variant "
                            f"(external_id={item.external_id!r}, sku={item.sku!r})."
                        ),
                    }
                },
            )

    item_dicts = [item.model_dump() for item in payload.products]
    deduped_items = list(
        dedup_last_wins(item_dicts, lambda r: resolve_identity(r, is_variant=False))
    )

    # Bucket (item, product-column-row) pairs by identity kind so the
    # RETURNING rows from each bucket's single statement can be matched
    # back to their originating item -- by identity value for the
    # ON-CONFLICT buckets (order not relied upon), positionally for the
    # identity-less bucket (a plain INSERT ... VALUES ... RETURNING with
    # no ON CONFLICT preserves input row order).
    buckets: dict[str | None, list[tuple[dict, dict]]] = {}
    for item in deduped_items:
        row = {
            "workspace_id": ws,
            "external_id": item.get("external_id"),
            "sku": item.get("sku"),
            "title": item["title"],
            "brand": item.get("brand"),
            "barcode": item.get("barcode"),
            "url": item.get("url"),
            "status": item.get("status") or "active",
        }
        identity = resolve_identity(item, is_variant=False)
        kind = identity[0] if identity is not None else None
        buckets.setdefault(kind, []).append((item, row))

    for item, product_id in _execute_product_buckets(session, buckets):
        item["_product_id"] = product_id
    session.flush()

    # Every dict below carries the same explicit key set (`barcode`,
    # `option_values`, ... default to None when the source doesn't have
    # them) so a single `.values([...])` batch mixing derived-default and
    # explicit-nested rows compiles as one consistent multi-row INSERT.
    variant_rows: list[dict] = []
    for item in deduped_items:
        product_id = item.get("_product_id")
        nested = item.get("variants") or []
        if not nested:
            default_variant = derive_default_variant(item)
            variant_rows.append(
                {
                    "workspace_id": ws,
                    "product_id": product_id,
                    "external_id": None,
                    "sku": default_variant["sku"],
                    "barcode": None,
                    "title": default_variant["title"],
                    "option_values": default_variant["option_values"],
                    "current_price": default_variant["current_price"],
                    "currency": default_variant["currency"],
                    "url": default_variant["url"],
                    "status": default_variant["status"],
                }
            )
        else:
            for v in nested:
                variant_rows.append(
                    {
                        "workspace_id": ws,
                        "product_id": product_id,
                        "external_id": v.get("external_id"),
                        "sku": v.get("sku"),
                        "barcode": v.get("barcode"),
                        "title": v["title"],
                        "option_values": v.get("option_values"),
                        "current_price": v["price"],
                        "currency": v["currency"],
                        "url": v.get("url"),
                        "status": v.get("status") or "active",
                    }
                )

    # Bounded (<=3) variant upsert statements -- executed for effect only;
    # the response re-fetches via `_variants_by_product_id` below (a
    # single scoped IN(...) select, not per-row).
    for stmt in plan_upsert(variant_rows, is_variant=True):
        session.execute(stmt)
    session.flush()

    product_ids = [item["_product_id"] for item in deduped_items]
    products = (
        session.execute(scoped_select(Product, ws).where(Product.id.in_(product_ids)))
        .scalars()
        .all()
    )
    grouped = _variants_by_product_id(session, ws, [p.id for p in products])
    responses = [_to_product_response(p, grouped.get(p.id, [])) for p in products]
    return ProductBulkUpsertResult(upserted=len(responses), products=responses)


def _execute_product_buckets(
    session, buckets: dict[str | None, list[tuple[dict, dict]]]
) -> list[tuple[dict, uuid.UUID]]:
    """Execute one `ON CONFLICT`/plain-insert statement per identity-kind bucket.

    Returns `(item, product_id)` pairs for every row across every
    bucket -- bounded at <=3 statements total (one per identity kind),
    never one statement per product (SC-003).
    """
    pairs: list[tuple[dict, uuid.UUID]] = []
    for kind, bucket in buckets.items():
        if not bucket:
            continue
        items = [pair[0] for pair in bucket]
        rows = [pair[1] for pair in bucket]
        stmt = build_products_upsert(rows, kind).returning(
            Product.id, Product.external_id, Product.sku
        )
        returned = session.execute(stmt).all()
        if kind is None:
            # Plain insert, no ON CONFLICT -> row order is preserved.
            for item, ret in zip(items, returned, strict=True):
                pairs.append((item, ret.id))
        else:
            by_identity: dict[tuple[str, str], uuid.UUID] = {}
            for ret in returned:
                if ret.external_id:
                    by_identity[("external_id", ret.external_id)] = ret.id
                if ret.sku:
                    by_identity[("sku", ret.sku)] = ret.id
            for item in items:
                identity = resolve_identity(item, is_variant=False)
                pairs.append((item, by_identity[identity]))
    return pairs


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
