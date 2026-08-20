"""`POST /v1/products/bulk-upsert` returns products in REQUEST order (audit B1).

The handler re-fetches the upserted rows with a single scoped
`WHERE id IN (...)` select and **no `ORDER BY`** — Postgres is free to
return those rows in any order it likes, and for a batch that mixes
freshly-inserted with updated rows it generally does not return them in
the order the client sent them. A client that maps the response
positionally back onto its request items (the SaaS control plane did
exactly that until 2026-08-20) would then write one product's id onto a
different product's record.

Two independent guarantees are asserted here, both drivable without a
database by handing the shipped handler a fake session that returns the
re-fetch rows in a deliberately WRONG order:

1. ordering — `products[i]` corresponds to `payload.products[i]`, for a
   batch that mixes an `external_id`-keyed (ON CONFLICT) bucket with an
   identity-less (plain INSERT) one;
2. echo — every response item carries its own `external_id`/`sku`, so a
   correct client never has to rely on position at all.

Deliberately drives the real `bulk_upsert_products` function (not a
re-implementation): only the SQLAlchemy `Session` is a double.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select
from sqlalchemy.sql.dml import Insert

from app.deps import Principal
from app.routers.products import bulk_upsert_products
from app.schemas.catalog import ProductBulkUpsertItem, ProductBulkUpsertRequest
from app_shared.enums import ProductStatus
from app_shared.models import Product

WORKSPACE_ID = uuid.uuid4()
_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


class _Returned:
    """One `RETURNING id, external_id, sku` row."""

    def __init__(self, id: uuid.UUID, external_id: str | None, sku: str | None) -> None:
        self.id = id
        self.external_id = external_id
        self.sku = sku


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> "_Result":
        return self

    def fetchall(self) -> list[Any]:
        return self._rows


class _ShuffledSession:
    """Session double whose product re-fetch answers in REVERSED id order.

    That is the whole point: a `WHERE id IN (...)` with no `ORDER BY`
    may legally answer in any order, so the fake picks the one order
    that is guaranteed to be wrong if the handler trusts the database.
    """

    def __init__(self, ids_by_identity: dict[tuple[str, str] | None, list[uuid.UUID]]) -> None:
        self._ids_by_identity = ids_by_identity
        self._insert_calls = 0
        self.products: dict[uuid.UUID, Product] = {}

    def execute(self, stmt: Any) -> _Result:
        if isinstance(stmt, Insert):
            self._insert_calls += 1
            returning = getattr(stmt, "_returning", ())
            if not returning:
                # A variant upsert — executed for effect only.
                return _Result([])
            rows: list[_Returned] = []
            for values in stmt._multi_values[0] if stmt._multi_values else stmt._values:
                row = {k.name if hasattr(k, "name") else k: v for k, v in values.items()}
                pid = uuid.uuid4()
                external_id = _literal(row.get("external_id"))
                sku = _literal(row.get("sku"))
                self.products[pid] = Product(
                    id=pid,
                    workspace_id=WORKSPACE_ID,
                    external_id=external_id,
                    sku=sku,
                    title=_literal(row.get("title")),
                    brand=None,
                    barcode=None,
                    url=None,
                    status=ProductStatus.ACTIVE,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
                rows.append(_Returned(pid, external_id, sku))
            return _Result(rows)

        assert isinstance(stmt, Select)
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Product:
            wanted = _in_values(stmt)
            # REVERSED — the DB is entitled to any order, so return the
            # one that breaks a positional client.
            return _Result([self.products[pid] for pid in reversed(wanted)])
        return _Result([])  # no variants

    def flush(self) -> None:
        return None


def _literal(value: Any) -> Any:
    return getattr(value, "value", value)


def _in_values(stmt: Select) -> list[uuid.UUID]:
    for clause in stmt.whereclause.clauses:  # type: ignore[union-attr]
        right = getattr(clause, "right", None)
        value = getattr(right, "value", None)
        if isinstance(value, (list, tuple)):
            return list(value)
    raise AssertionError("no IN(...) predicate found on the product re-fetch")


def _principal_ctx(session: _ShuffledSession) -> tuple[Any, Principal]:
    return session, Principal(
        kind="api_key",
        id=uuid.uuid4(),
        role=None,
        scopes=["products:write"],
        workspace_id=WORKSPACE_ID,
    )


def _payload() -> ProductBulkUpsertRequest:
    return ProductBulkUpsertRequest(
        products=[
            ProductBulkUpsertItem(
                external_id="EXT-A", title="Alpha", price="10.00", currency="SAR"
            ),
            ProductBulkUpsertItem(title="Bravo (identity-less)", price="20.00", currency="SAR"),
            ProductBulkUpsertItem(
                external_id="EXT-C", title="Charlie", price="30.00", currency="SAR"
            ),
            ProductBulkUpsertItem(sku="SKU-D", title="Delta", price="40.00", currency="SAR"),
        ]
    )


def test_mixed_batch_response_is_in_request_order() -> None:
    session = _ShuffledSession({})
    payload = _payload()

    result = bulk_upsert_products(payload=payload, principal_ctx=_principal_ctx(session))

    assert result.upserted == 4
    assert [p.title for p in result.products] == [
        "Alpha",
        "Bravo (identity-less)",
        "Charlie",
        "Delta",
    ]


def test_every_response_item_echoes_its_own_identity() -> None:
    session = _ShuffledSession({})
    payload = _payload()

    result = bulk_upsert_products(payload=payload, principal_ctx=_principal_ctx(session))

    assert [p.external_id for p in result.products] == ["EXT-A", None, "EXT-C", None]
    assert [p.sku for p in result.products] == [None, None, None, "SKU-D"]
    # ids are distinct — the ordering fix must not collapse two items onto one row
    assert len({p.id for p in result.products}) == 4
