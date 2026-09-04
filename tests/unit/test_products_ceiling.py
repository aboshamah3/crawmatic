"""The entitlement product ceiling, enforced at the engine boundary (EPA C2).

`workspace_entitlements.product_ceiling` is the plan's cap on how many
products a tenant may monitor. Until now the engine recorded it and
nothing read it, so a tenant on a 25-product plan could push 25 000 and
the engine would happily crawl all of them — the cap existed only as a
number in a row, which is to say it did not exist.

This file pins the boundary in both directions:

* **NULL is not a cap.** C1 made the column nullable precisely so an
  older evidence row (or a plan that expresses no cap) reads as "no
  ceiling recorded", never as zero. The check must not even QUERY the
  catalog in that case, and the tests below count queries to prove it —
  every catalog write in the product would otherwise pay for a
  `COUNT(*)` to learn there is nothing to enforce.
* **`0` is a real cap.** A plan that allows zero products must refuse
  the first one.

Bulk upsert counts only NEW products: re-pushing an unchanged catalog is
the steady-state shape of a connector sync, and counting updates as
additions would refuse every one of them at exactly the ceiling.

Drives the shipped handlers (`app.routers.products.create_product` /
`bulk_upsert_products`) and the shipped service, with a session double —
the `test_bulk_upsert_response_order.py` pattern.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Select
from sqlalchemy.sql.functions import Function

from app.deps import Principal
from app.routers.products import bulk_upsert_products, create_product
from app.schemas.catalog import (
    ProductBulkUpsertItem,
    ProductBulkUpsertRequest,
    ProductCreate,
)
from app_shared.control_plane import service
from app_shared.models.catalog import Product
from app_shared.models.cost_authorization import EntitlementState, WorkspaceEntitlement

WORKSPACE_ID = uuid.uuid4()
_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _entitlement(ceiling: int | None) -> WorkspaceEntitlement:
    return WorkspaceEntitlement(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        state=EntitlementState.ACTIVE,
        plan_code="starter",
        evidence_version="5",
        product_ceiling=ceiling,
        observed_at=_NOW,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0][0] if self._rows else None


class _CeilingSession:
    """Answers exactly the three statements the ceiling check issues.

    Records every statement so a test can assert the CHEAP PATH: with no
    ceiling to enforce, the catalog is never counted and no identity is
    ever looked up.
    """

    def __init__(
        self,
        *,
        entitlement: WorkspaceEntitlement | None,
        active: int = 0,
        existing_external: tuple[str, ...] = (),
        existing_sku: tuple[str, ...] = (),
    ) -> None:
        self.entitlement = entitlement
        self.active = active
        self.existing_external = existing_external
        self.existing_sku = existing_sku
        self.statements: list[Any] = []
        self.added: list[Any] = []

    def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        assert isinstance(stmt, Select), "the ceiling check issues SELECTs only"
        description = stmt.column_descriptions[0]

        if description["entity"] is WorkspaceEntitlement:
            return _Result([self.entitlement] if self.entitlement else [])
        if isinstance(description["expr"], Function):
            return _Result([(self.active,)])
        if description["name"] == "external_id":
            return _Result([(value,) for value in self.existing_external])
        if description["name"] == "sku":
            return _Result([(value,) for value in self.existing_sku])
        raise AssertionError(f"unexpected statement: {stmt}")

    # --- the create path's write seam, so an ALLOWED create completes ---
    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: Any) -> None:
        for obj in objs:
            self.add(obj)

    def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = _NOW
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = _NOW

    def counted_the_catalog(self) -> bool:
        return any(
            isinstance(stmt.column_descriptions[0]["expr"], Function)
            for stmt in self.statements
        )


def _principal_ctx(session: _CeilingSession) -> tuple[Any, Principal]:
    return session, Principal(
        kind="api_key",
        id=uuid.uuid4(),
        role=None,
        scopes=["products:write"],
        workspace_id=WORKSPACE_ID,
    )


def _create_payload() -> ProductCreate:
    return ProductCreate(
        external_id="EXT-NEW", title="Widget", price="10.00", currency="SAR"
    )


def _bulk_payload(*external_ids: str) -> ProductBulkUpsertRequest:
    return ProductBulkUpsertRequest(
        products=[
            ProductBulkUpsertItem(
                external_id=external_id,
                title=f"Product {external_id}",
                price="10.00",
                currency="SAR",
            )
            for external_id in external_ids
        ]
    )


# --- the service verdict ------------------------------------------------


def test_no_entitlement_row_means_no_ceiling_and_no_catalog_query() -> None:
    session = _CeilingSession(entitlement=None, active=999)
    verdict = service.check_product_ceiling(session, WORKSPACE_ID, requested=1)
    assert verdict.exceeded is False
    assert session.counted_the_catalog() is False


def test_a_null_ceiling_means_no_ceiling_and_no_catalog_query() -> None:
    session = _CeilingSession(entitlement=_entitlement(None), active=999)
    verdict = service.check_product_ceiling(session, WORKSPACE_ID, requested=1)
    assert verdict.exceeded is False
    assert verdict.ceiling is None
    assert session.counted_the_catalog() is False


def test_a_zero_ceiling_refuses_the_first_product() -> None:
    session = _CeilingSession(entitlement=_entitlement(0), active=0)
    verdict = service.check_product_ceiling(session, WORKSPACE_ID, requested=1)
    assert verdict.exceeded is True
    assert (verdict.ceiling, verdict.active, verdict.requested) == (0, 0, 1)


def test_exactly_at_the_ceiling_is_allowed() -> None:
    session = _CeilingSession(entitlement=_entitlement(25), active=24)
    assert service.check_product_ceiling(
        session, WORKSPACE_ID, requested=1
    ).exceeded is False


def test_one_past_the_ceiling_is_refused() -> None:
    session = _CeilingSession(entitlement=_entitlement(25), active=25)
    assert service.check_product_ceiling(
        session, WORKSPACE_ID, requested=1
    ).exceeded is True


def test_bulk_counts_only_products_that_do_not_exist_yet() -> None:
    session = _CeilingSession(
        entitlement=_entitlement(3), active=3, existing_external=("A", "B", "C")
    )
    verdict = service.check_product_ceiling_for_upsert(
        session,
        WORKSPACE_ID,
        [("external_id", "A"), ("external_id", "B"), ("external_id", "C")],
    )
    assert verdict.exceeded is False
    assert verdict.requested == 0
    # Nothing new -> the catalog count is never even issued.
    assert session.counted_the_catalog() is False


def test_bulk_refuses_when_the_new_products_would_pass_the_ceiling() -> None:
    session = _CeilingSession(
        entitlement=_entitlement(3), active=3, existing_external=("A",)
    )
    verdict = service.check_product_ceiling_for_upsert(
        session,
        WORKSPACE_ID,
        [("external_id", "A"), ("external_id", "D")],
    )
    assert verdict.exceeded is True
    assert (verdict.ceiling, verdict.active, verdict.requested) == (3, 3, 1)


def test_an_identity_less_bulk_item_always_counts_as_new() -> None:
    """FR-011: a product with neither `external_id` nor `sku` has nothing
    to match on and is inserted fresh on every call — so it always
    consumes a seat."""
    session = _CeilingSession(entitlement=_entitlement(1), active=1)
    verdict = service.check_product_ceiling_for_upsert(
        session, WORKSPACE_ID, [None]
    )
    assert verdict.exceeded is True
    assert verdict.requested == 1


def test_sku_identities_are_matched_on_sku_not_external_id() -> None:
    session = _CeilingSession(
        entitlement=_entitlement(5), active=1, existing_sku=("SKU-1",)
    )
    verdict = service.check_product_ceiling_for_upsert(
        session, WORKSPACE_ID, [("sku", "SKU-1"), ("sku", "SKU-2")]
    )
    assert verdict.requested == 1
    assert verdict.exceeded is False


# --- the routes ---------------------------------------------------------


def test_create_product_over_the_ceiling_is_409() -> None:
    session = _CeilingSession(entitlement=_entitlement(2), active=2)

    with pytest.raises(HTTPException) as excinfo:
        create_product(
            payload=_create_payload(), principal_ctx=_principal_ctx(session)
        )

    assert excinfo.value.status_code == 409
    error = excinfo.value.detail["error"]
    assert error["code"] == "PRODUCT_CEILING_EXCEEDED"
    assert error["ceiling"] == 2
    assert error["active"] == 2
    assert error["requested"] == 1
    assert session.added == [], "nothing may be written once the cap refuses"


def test_create_product_under_the_ceiling_is_allowed() -> None:
    session = _CeilingSession(entitlement=_entitlement(25), active=3)

    response = create_product(
        payload=_create_payload(), principal_ctx=_principal_ctx(session)
    )

    assert response.external_id == "EXT-NEW"
    assert [type(o).__name__ for o in session.added].count("Product") == 1


def test_create_product_with_no_entitlement_row_is_allowed() -> None:
    session = _CeilingSession(entitlement=None)

    response = create_product(
        payload=_create_payload(), principal_ctx=_principal_ctx(session)
    )

    assert response.title == "Widget"
    assert session.counted_the_catalog() is False


def test_bulk_upsert_over_the_ceiling_is_409_before_any_write() -> None:
    session = _CeilingSession(entitlement=_entitlement(3), active=2)

    with pytest.raises(HTTPException) as excinfo:
        bulk_upsert_products(
            payload=_bulk_payload("A", "B"), principal_ctx=_principal_ctx(session)
        )

    assert excinfo.value.status_code == 409
    error = excinfo.value.detail["error"]
    assert error["code"] == "PRODUCT_CEILING_EXCEEDED"
    assert (error["ceiling"], error["active"], error["requested"]) == (3, 2, 2)
    # The refusal lands BEFORE the upsert statements — the double would
    # have raised on an INSERT, and the session recorded none.
    assert session.added == []


def test_bulk_upsert_of_an_unchanged_catalog_at_the_ceiling_is_allowed() -> None:
    """The steady-state connector sync: every product already exists, the
    tenant sits exactly at its cap, and the push must still go through."""
    session = _CeilingSession(
        entitlement=_entitlement(2), active=2, existing_external=("A", "B")
    )

    verdict = service.check_product_ceiling_for_upsert(
        session, WORKSPACE_ID, [("external_id", "A"), ("external_id", "B")]
    )
    assert verdict.exceeded is False


def test_an_empty_bulk_batch_is_never_refused() -> None:
    session = _CeilingSession(entitlement=_entitlement(0), active=0)

    result = bulk_upsert_products(
        payload=ProductBulkUpsertRequest(products=[]),
        principal_ctx=_principal_ctx(session),
    )

    assert result.upserted == 0
    assert session.statements == []


def test_the_ceiling_denominator_is_active_products() -> None:
    """Archived products do not consume a seat — the count predicate must
    name `ProductStatus.ACTIVE` explicitly."""
    session = _CeilingSession(entitlement=_entitlement(5), active=1)
    service.check_product_ceiling(session, WORKSPACE_ID, requested=1)

    count_stmt = next(
        stmt
        for stmt in session.statements
        if isinstance(stmt.column_descriptions[0]["expr"], Function)
    )
    compiled = str(count_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "products.status" in compiled
    assert "active" in compiled
    assert "products.workspace_id" in compiled
    assert Product.__tablename__ == "products"
