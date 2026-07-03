"""Variants endpoints (`contracts/api-variants.md`) — SPEC-04 US1.

Variants are created via their parent product (`POST /v1/products`) or
bulk-upsert (US2, later); this router exposes read + update only — no
standalone create, no delete (a delete that could orphan a product down
to zero variants is deliberately absent from this feature; see
[analyze F2] note on `PATCH` below).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app_shared.models.catalog import ProductVariant
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.catalog import VariantListResponse, VariantResponse, VariantUpdate

router = APIRouter(prefix="/v1/variants", tags=["variants"])


@router.get("", response_model=VariantListResponse)
def list_variants(
    limit: int | None = None,
    cursor: str | None = None,
    product_id: uuid.UUID | None = None,
    principal_ctx: tuple = Depends(require_scopes("variants:read")),
) -> VariantListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(ProductVariant, principal.workspace_id)
    if product_id is not None:
        stmt = stmt.where(ProductVariant.product_id == product_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ProductVariant, after))
    stmt = stmt.order_by(ProductVariant.created_at, ProductVariant.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [VariantResponse.model_validate(v) for v in envelope["items"]]
    return VariantListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{variant_id}", response_model=VariantResponse)
def get_variant(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("variants:read")),
) -> VariantResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )
    return VariantResponse.model_validate(variant)


@router.patch("/{variant_id}", response_model=VariantResponse)
def update_variant(
    variant_id: uuid.UUID,
    payload: VariantUpdate,
    principal_ctx: tuple = Depends(require_scopes("variants:write")),
) -> VariantResponse:
    # [analyze F2] No variant-DELETE endpoint exists in this feature, so
    # this PATCH can never drop a product to zero variants — the FR-006
    # last-variant invariant is a structural guard maintained by the
    # catalog service (ensure_at_least_one, unit-tested in T009/T010),
    # not a runtime check here. Deliberately no zero-variant 409 path.
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    updates = payload.model_dump(exclude_unset=True)
    # "price" (schema/API name) maps to the "current_price" ORM column.
    if "price" in updates:
        updates["current_price"] = updates.pop("price")
    for field, value in updates.items():
        setattr(variant, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "CONFLICT",
                    "message": (
                        "This title is already used by another variant of the "
                        "same product (unique(workspace_id, product_id, title))."
                    ),
                }
            },
        ) from exc

    return VariantResponse.model_validate(variant)
