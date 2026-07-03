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
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.catalog.upsert import plan_upsert
from app_shared.models.alerts import VariantPriceState
from app_shared.models.catalog import Product, ProductVariant
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.alerts import PriceComparisonResponse
from app.schemas.catalog import (
    VariantBulkUpsertResult,
    VariantListResponse,
    VariantResponse,
    VariantsBulkUpsertRequest,
    VariantUpdate,
)

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


@router.get("/{variant_id}/price-comparison", response_model=PriceComparisonResponse)
def get_price_comparison(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> PriceComparisonResponse:
    """`GET /v1/variants/{variant_id}/price-comparison` (SPEC-09 US1, FR-017/FR-020).

    404s an unknown/cross-workspace variant (checked first, via
    `scoped_get`) and, separately, a variant that has never been
    analyzed yet (no `variant_price_states` row) — both distinguishable
    only by message, not status code (contracts/api-alerts.md).
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    price_state = session.execute(
        scoped_select(VariantPriceState, principal.workspace_id).where(
            VariantPriceState.product_variant_id == variant_id
        )
    ).scalar_one_or_none()
    if price_state is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "No price comparison has been computed yet for this variant.",
                }
            },
        )

    return PriceComparisonResponse(
        product_variant_id=price_state.product_variant_id,
        client_price=price_state.client_price,
        currency=price_state.currency,
        cheapest_competitor_price=price_state.cheapest_competitor_price,
        average_competitor_price=price_state.average_competitor_price,
        highest_competitor_price=price_state.highest_competitor_price,
        comparable_competitor_count=price_state.comparable_competitor_count,
        alert_type=price_state.latest_alert_type,
        alert_severity=price_state.latest_alert_severity,
        calculated_at=price_state.calculated_at,
    )


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


def _resolve_parent_product_ids(
    session, workspace_id: uuid.UUID, item_dicts: list[dict]
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    """One scoped lookup for external_id/sku parent refs, one narrow id-in(...)
    lookup for explicit `product_id` refs (contracts/catalog-bulk-upsert.md
    "Variant->product resolution").

    Returns `(by_external_id, by_sku, workspace_by_explicit_id)` --
    the last is a `{id: workspace_id}` map (not filtered to this
    workspace) so `app_shared.catalog.consistency.assert_refs_in_workspace`
    can distinguish a cross-workspace `product_id` from a nonexistent one.
    """
    external_ids = {i["product_external_id"] for i in item_dicts if i.get("product_external_id")}
    skus = {i["product_sku"] for i in item_dicts if i.get("product_sku")}
    explicit_ids = {i["product_id"] for i in item_dicts if i.get("product_id") is not None}

    by_external_id: dict[str, uuid.UUID] = {}
    by_sku: dict[str, uuid.UUID] = {}
    if external_ids or skus:
        conditions = []
        if external_ids:
            conditions.append(Product.external_id.in_(external_ids))
        if skus:
            conditions.append(Product.sku.in_(skus))
        rows = session.execute(scoped_select(Product, workspace_id).where(or_(*conditions))).scalars().all()
        for p in rows:
            if p.external_id:
                by_external_id[p.external_id] = p.id
            if p.sku:
                by_sku[p.sku] = p.id

    workspace_by_explicit_id: dict[uuid.UUID, uuid.UUID] = {}
    if explicit_ids:
        # Narrow, fixed-column, id-in(...) lookup limited to exactly the
        # referenced ids -- intentionally workspace-unscoped so a
        # cross-workspace product_id can be told apart from a nonexistent
        # one (Layer 2 of the two-layer model, see consistency.md); every
        # id is then re-checked against `workspace_id` via
        # `assert_refs_in_workspace` before it's trusted.
        rows = session.execute(
            select(Product.id, Product.workspace_id).where(Product.id.in_(explicit_ids))  # noqa: workspace-scope
        ).all()
        workspace_by_explicit_id = {row.id: row.workspace_id for row in rows}

    return by_external_id, by_sku, workspace_by_explicit_id


@router.post("/bulk-upsert", response_model=VariantBulkUpsertResult, status_code=200)
def bulk_upsert_variants(
    payload: VariantsBulkUpsertRequest,
    principal_ctx: tuple = Depends(require_scopes("variants:write")),
) -> VariantBulkUpsertResult:
    """Set-based standalone variant bulk upsert (`contracts/catalog-bulk-upsert.md`).

    Each row names its parent product by `product_id` /
    `product_external_id` / `product_sku`; parent resolution is one
    scoped lookup (never per-row). A cross-workspace or unresolvable
    parent reference is rejected (422) via the workspace-consistency
    pre-check (FR-009) before any upsert statement runs.
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if not payload.variants:
        return VariantBulkUpsertResult(upserted=0, variants=[])

    item_dicts = [v.model_dump() for v in payload.variants]
    by_external_id, by_sku, workspace_by_explicit_id = _resolve_parent_product_ids(
        session, ws, item_dicts
    )

    resolved_rows: list[dict] = []
    unresolved: list[dict] = []
    for item in item_dicts:
        product_id: uuid.UUID | None = None
        if item.get("product_id") is not None:
            try:
                assert_refs_in_workspace(ws, [item["product_id"]], workspace_by_explicit_id)
                product_id = item["product_id"]
            except (CrossWorkspaceReference, MissingReference):
                product_id = None
        elif item.get("product_external_id"):
            product_id = by_external_id.get(item["product_external_id"])
        elif item.get("product_sku"):
            product_id = by_sku.get(item["product_sku"])

        if product_id is None:
            unresolved.append(item)
            continue
        item["product_id"] = product_id
        resolved_rows.append(item)

    if unresolved:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "UNRESOLVED_PARENT",
                    "message": (
                        "One or more variant rows reference a product_id/"
                        "product_external_id/product_sku that does not "
                        "resolve to a product in this workspace."
                    ),
                    "count": len(unresolved),
                }
            },
        )

    variant_rows = [
        {
            "workspace_id": ws,
            "product_id": r["product_id"],
            "external_id": r.get("external_id"),
            "sku": r.get("sku"),
            "barcode": r.get("barcode"),
            "title": r["title"],
            "option_values": r.get("option_values"),
            "current_price": r["price"],
            "currency": r["currency"],
            "url": r.get("url"),
            "status": r.get("status") or "active",
        }
        for r in resolved_rows
    ]

    variant_ids: list[uuid.UUID] = []
    for stmt in plan_upsert(variant_rows, is_variant=True):
        stmt = stmt.returning(ProductVariant.id)
        variant_ids.extend(row.id for row in session.execute(stmt).all())
    session.flush()

    variants = (
        session.execute(scoped_select(ProductVariant, ws).where(ProductVariant.id.in_(variant_ids)))
        .scalars()
        .all()
        if variant_ids
        else []
    )
    return VariantBulkUpsertResult(
        upserted=len(variants), variants=[VariantResponse.model_validate(v) for v in variants]
    )
