"""Product-groups endpoints (`contracts/api-product-groups.md`) — SPEC-04 US3.

Same auth seam/scope-gating discipline as `routers/products.py` /
`routers/variants.py` (`app.deps.require_scopes`, `scoped_select`/
`scoped_get`). Group management reuses the resource write scopes — no
dedicated group scope exists in the vocabulary (§33): group
create/update/delete and add/remove-item require `products:write`; when
the item being added is a *variant*, `variants:write` is ALSO required
(checked explicitly in `add_group_item`, since the required scope set
depends on the request body, not just the route). Reads require
`products:read`.

Item references are workspace-consistency pre-checked
(`app_shared.catalog.consistency`, `contracts/workspace-consistency.md`
Layer 2) before any insert, so a cross-workspace/nonexistent reference
answers a clean `422`/`404` instead of a raw `IntegrityError` (500);
duplicate membership (the partial-unique index on
`(workspace_id, product_group_id, product_id)` /
`(workspace_id, product_group_id, product_variant_id)`) is caught as a
`409`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.scopes import has_scopes

from app.deps import Principal, require_scopes
from app.schemas.catalog import (
    DeleteOutcome,
    GroupCreate,
    GroupItemCreate,
    GroupItemResponse,
    GroupListResponse,
    GroupResponse,
    GroupUpdate,
)

router = APIRouter(prefix="/v1/product-groups", tags=["product-groups"])


def _forbidden(message: str) -> HTTPException:
    """A `403` for a missing-scope failure (mirrors `app.deps._forbidden`)."""
    return HTTPException(
        status_code=403, detail={"error": {"code": "FORBIDDEN", "message": message}}
    )


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": {"code": "CONFLICT", "message": message}})


def _items_by_group_id(
    session, workspace_id: uuid.UUID, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProductGroupItem]]:
    """Bulk-fetch every item for `group_ids`, grouped — avoids an N+1 query."""
    if not group_ids:
        return {}
    rows = (
        session.execute(
            scoped_select(ProductGroupItem, workspace_id).where(
                ProductGroupItem.product_group_id.in_(group_ids)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ProductGroupItem]] = {}
    for row in rows:
        grouped.setdefault(row.product_group_id, []).append(row)
    return grouped


def _to_group_response(group: ProductGroup, items: list[ProductGroupItem]) -> GroupResponse:
    return GroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        status=group.status,
        created_at=group.created_at,
        updated_at=group.updated_at,
        items=[GroupItemResponse.model_validate(i) for i in items],
    )


@router.post("", response_model=GroupResponse, status_code=201)
def create_group(
    payload: GroupCreate,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> GroupResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    group = ProductGroup(
        workspace_id=principal.workspace_id,
        name=payload.name,
        description=payload.description,
        status=payload.status,
    )
    session.add(group)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(
            "A group with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return _to_group_response(group, [])


@router.get("", response_model=GroupListResponse)
def list_groups(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("products:read")),
) -> GroupListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(ProductGroup, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ProductGroup, after))
    stmt = stmt.order_by(ProductGroup.created_at, ProductGroup.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    groups: list[ProductGroup] = envelope["items"]

    grouped = _items_by_group_id(session, principal.workspace_id, [g.id for g in groups])
    items = [_to_group_response(g, grouped.get(g.id, [])) for g in groups]
    return GroupListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{group_id}", response_model=GroupResponse)
def get_group(
    group_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("products:read")),
) -> GroupResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    group = scoped_get(session, ProductGroup, group_id, principal.workspace_id)
    if group is None:
        raise _not_found("Group not found.")

    grouped = _items_by_group_id(session, principal.workspace_id, [group.id])
    return _to_group_response(group, grouped.get(group.id, []))


@router.patch("/{group_id}", response_model=GroupResponse)
def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> GroupResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    group = scoped_get(session, ProductGroup, group_id, principal.workspace_id)
    if group is None:
        raise _not_found("Group not found.")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(group, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(
            "A group with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    grouped = _items_by_group_id(session, principal.workspace_id, [group.id])
    return _to_group_response(group, grouped.get(group.id, []))


@router.delete("/{group_id}", response_model=DeleteOutcome)
def delete_group(
    group_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    group = scoped_get(session, ProductGroup, group_id, principal.workspace_id)
    if group is None:
        raise _not_found("Group not found.")

    # No dependent history exists in this spec -> hard delete (FR-017),
    # structured so a future archive-by-status path only needs to swap
    # the branch below for `group.status = GroupStatus.ARCHIVED`.
    # Children must go first (FK product_groups <- product_group_items).
    session.execute(
        delete(ProductGroupItem).where(
            ProductGroupItem.workspace_id == principal.workspace_id,
            ProductGroupItem.product_group_id == group.id,
        )
    )
    session.delete(group)
    session.flush()

    return DeleteOutcome(id=group_id, outcome="hard_deleted")


def _lookup_workspace_for_id(session, model: type, id_: uuid.UUID) -> uuid.UUID | None:
    """Single, intentionally workspace-unscoped id lookup.

    Needed so a cross-workspace reference (`CrossWorkspaceReference`,
    422) can be told apart from a nonexistent one (`MissingReference`,
    404) — the same two-layer pattern as
    `routers/variants.py::_resolve_parent_product_ids`'s explicit
    `product_id` branch.
    """
    row = session.execute(
        select(model.id, model.workspace_id).where(model.id == id_)  # noqa: workspace-scope
    ).first()
    return row.workspace_id if row is not None else None


@router.post("/{group_id}/items", response_model=GroupItemResponse, status_code=201)
def add_group_item(
    group_id: uuid.UUID,
    payload: GroupItemCreate,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> GroupItemResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    # `GroupItemCreate` already guarantees exactly one of the two refs is
    # set (`exactly_one_of`, app_shared.catalog.consistency).
    is_variant_item = payload.product_variant_id is not None
    if is_variant_item and not has_scopes(principal.scopes, ["variants:write"]):
        raise _forbidden(
            "Adding a variant item to a group also requires the "
            "variants:write scope."
        )

    group = scoped_get(session, ProductGroup, group_id, ws)
    if group is None:
        raise _not_found("Group not found.")

    if is_variant_item:
        model: type = ProductVariant
        ref_id = payload.product_variant_id
    else:
        model = Product
        ref_id = payload.product_id
    assert ref_id is not None

    actual_workspace_id = _lookup_workspace_for_id(session, model, ref_id)
    resolved = {ref_id: actual_workspace_id} if actual_workspace_id is not None else {}
    try:
        assert_refs_in_workspace(ws, [ref_id], resolved)
    except MissingReference as exc:
        raise _not_found(
            f"{model.__name__} referenced by this group item does not exist."
        ) from exc
    except CrossWorkspaceReference as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CROSS_WORKSPACE_REFERENCE",
                    "message": (
                        f"{model.__name__} referenced by this group item "
                        "belongs to a different workspace."
                    ),
                }
            },
        ) from exc

    item = ProductGroupItem(
        workspace_id=ws,
        product_group_id=group_id,
        product_id=payload.product_id,
        product_variant_id=payload.product_variant_id,
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(
            "This product/variant is already a member of this group "
            "(duplicate membership)."
        ) from exc

    return GroupItemResponse.model_validate(item)


@router.delete("/{group_id}/items/{item_id}", status_code=204)
def remove_group_item(
    group_id: uuid.UUID,
    item_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("products:write")),
) -> None:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    item = scoped_get(session, ProductGroupItem, item_id, principal.workspace_id)
    if item is None or item.product_group_id != group_id:
        raise _not_found("Group item not found.")

    session.delete(item)
    session.flush()
    return None
