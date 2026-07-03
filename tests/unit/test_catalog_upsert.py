"""Unit tests for `app_shared.catalog.upsert` (T024, FR-010/011/012, SC-002/003).

Pure, DB-independent -- compiles every statement to `postgresql`-dialect
SQL text (`str(stmt.compile(dialect=postgresql.dialect()))`) and asserts
on the rendered `ON CONFLICT (...) [WHERE ...] DO UPDATE SET ...`
clause; never opens a session or hits a database.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from app_shared.catalog.upsert import (
    build_products_upsert,
    build_variants_upsert,
    dedup_last_wins,
    inject_default_variants,
    plan_upsert,
    resolve_identity,
    resolve_variant_parents,
    variant_parent_lookup_keys,
)

WS = uuid.uuid4()


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- resolve_identity: precedence external_id > sku > (product_id, title) ----


def test_resolve_identity_prefers_external_id_over_sku() -> None:
    row = {"external_id": "EXT-1", "sku": "SKU-1"}
    assert resolve_identity(row) == ("external_id", "EXT-1")


def test_resolve_identity_falls_back_to_sku_when_no_external_id() -> None:
    row = {"external_id": None, "sku": "SKU-1"}
    assert resolve_identity(row) == ("sku", "SKU-1")


def test_resolve_identity_product_has_no_third_fallback() -> None:
    row = {"external_id": None, "sku": None, "title": "Widget"}
    assert resolve_identity(row, is_variant=False) is None


def test_resolve_identity_variant_falls_back_to_product_title() -> None:
    pid = uuid.uuid4()
    row = {"external_id": None, "sku": None, "product_id": pid, "title": "Red"}
    assert resolve_identity(row, is_variant=True) == ("product_title", (pid, "Red"))


def test_resolve_identity_variant_prefers_external_id_over_product_title() -> None:
    pid = uuid.uuid4()
    row = {"external_id": "EXT-V1", "sku": None, "product_id": pid, "title": "Red"}
    assert resolve_identity(row, is_variant=True) == ("external_id", "EXT-V1")


def test_resolve_identity_variant_prefers_sku_over_product_title() -> None:
    pid = uuid.uuid4()
    row = {"external_id": None, "sku": "SKU-V1", "product_id": pid, "title": "Red"}
    assert resolve_identity(row, is_variant=True) == ("sku", "SKU-V1")


def test_resolve_identity_none_when_nothing_resolves() -> None:
    row = {"external_id": None, "sku": None, "title": None}
    assert resolve_identity(row) is None
    assert resolve_identity(row, is_variant=True) is None


# --- dedup_last_wins: keeps the last of colliding-identity rows --------------


def test_dedup_last_wins_keeps_last_row_for_same_identity() -> None:
    rows = [
        {"external_id": "A", "title": "first"},
        {"external_id": "B", "title": "other"},
        {"external_id": "A", "title": "last"},
    ]
    result = dedup_last_wins(rows, resolve_identity)
    assert len(result) == 2
    # Stable position: "A" keeps its original slot (index 0), now holding
    # the LAST occurrence's data.
    assert result[0] == {"external_id": "A", "title": "last"}
    assert result[1] == {"external_id": "B", "title": "other"}


def test_dedup_last_wins_never_collapses_identity_less_rows() -> None:
    rows = [
        {"external_id": None, "sku": None, "title": "no-id-1"},
        {"external_id": None, "sku": None, "title": "no-id-2"},
    ]
    result = dedup_last_wins(rows, resolve_identity)
    assert len(result) == 2


# --- build_products_upsert: compiled SQL per identity kind --------------------


def test_build_products_upsert_external_id_compiles_partial_on_conflict() -> None:
    rows = [
        {
            "workspace_id": WS,
            "external_id": "EXT-1",
            "sku": None,
            "title": "Widget",
            "brand": None,
            "barcode": None,
            "url": None,
            "status": "active",
        }
    ]
    stmt = build_products_upsert(rows, "external_id")
    sql = _compiled(stmt)

    assert "INSERT INTO products" in sql
    assert "ON CONFLICT (workspace_id, external_id)" in sql
    assert "WHERE external_id IS NOT NULL" in sql
    assert "DO UPDATE SET" in sql
    # Never overwrite id/workspace_id/created_at from `excluded`.
    assert "id = excluded.id" not in sql
    assert "workspace_id = excluded.workspace_id" not in sql
    assert "created_at = excluded.created_at" not in sql
    # Updatable columns present.
    assert "title = excluded.title" in sql
    assert "status = excluded.status" in sql


def test_build_products_upsert_sku_compiles_partial_on_conflict() -> None:
    rows = [
        {
            "workspace_id": WS,
            "external_id": None,
            "sku": "SKU-1",
            "title": "Widget",
            "brand": None,
            "barcode": None,
            "url": None,
            "status": "active",
        }
    ]
    stmt = build_products_upsert(rows, "sku")
    sql = _compiled(stmt)

    assert "ON CONFLICT (workspace_id, sku)" in sql
    assert "WHERE sku IS NOT NULL" in sql
    assert "DO UPDATE SET" in sql


def test_build_products_upsert_identity_less_is_a_plain_insert_always() -> None:
    rows = [
        {
            "workspace_id": WS,
            "external_id": None,
            "sku": None,
            "title": "No Identity Widget",
            "brand": None,
            "barcode": None,
            "url": None,
            "status": "active",
        }
    ]
    stmt = build_products_upsert(rows, None)
    sql = _compiled(stmt)

    assert "INSERT INTO products" in sql
    assert "ON CONFLICT" not in sql
    assert "DO UPDATE" not in sql


# --- build_variants_upsert: compiled SQL, including the full-unique kind -----


def test_build_variants_upsert_product_title_compiles_full_unique_on_conflict() -> None:
    pid = uuid.uuid4()
    rows = [
        {
            "workspace_id": WS,
            "product_id": pid,
            "external_id": None,
            "sku": None,
            "barcode": None,
            "title": "Red",
            "option_values": None,
            "current_price": Decimal("9.99"),
            "currency": "USD",
            "url": None,
            "status": "active",
        }
    ]
    stmt = build_variants_upsert(rows, "product_title")
    sql = _compiled(stmt)

    assert "INSERT INTO product_variants" in sql
    assert "ON CONFLICT (workspace_id, product_id, title)" in sql
    # Full unique -> no WHERE predicate on the arbiter clause.
    assert "ON CONFLICT (workspace_id, product_id, title) DO UPDATE SET" in sql
    assert "current_price = excluded.current_price" in sql
    assert "currency = excluded.currency" in sql


def test_build_variants_upsert_external_id_compiles_partial_on_conflict() -> None:
    pid = uuid.uuid4()
    rows = [
        {
            "workspace_id": WS,
            "product_id": pid,
            "external_id": "VEXT-1",
            "sku": None,
            "barcode": None,
            "title": "Red",
            "option_values": None,
            "current_price": Decimal("9.99"),
            "currency": "USD",
            "url": None,
            "status": "active",
        }
    ]
    stmt = build_variants_upsert(rows, "external_id")
    sql = _compiled(stmt)

    assert "ON CONFLICT (workspace_id, external_id)" in sql
    assert "WHERE external_id IS NOT NULL" in sql


# --- plan_upsert: bounded statement count, no per-row loop --------------------


def test_plan_upsert_products_bounded_at_three_statements_for_large_batch() -> None:
    rows = []
    for i in range(300):
        rows.append(
            {
                "workspace_id": WS,
                "external_id": f"EXT-{i}",
                "sku": None,
                "title": f"Widget {i}",
                "brand": None,
                "barcode": None,
                "url": None,
                "status": "active",
            }
        )
    for i in range(300):
        rows.append(
            {
                "workspace_id": WS,
                "external_id": None,
                "sku": f"SKU-{i}",
                "title": f"Widget {i}",
                "brand": None,
                "barcode": None,
                "url": None,
                "status": "active",
            }
        )
    for i in range(300):
        rows.append(
            {
                "workspace_id": WS,
                "external_id": None,
                "sku": None,
                "title": f"No Identity Widget {i}",
                "brand": None,
                "barcode": None,
                "url": None,
                "status": "active",
            }
        )

    statements = plan_upsert(rows, is_variant=False)
    assert len(statements) <= 3
    assert len(statements) == 3


def test_plan_upsert_variants_bounded_at_three_statements_for_large_batch() -> None:
    rows = []
    for i in range(200):
        rows.append(
            {
                "workspace_id": WS,
                "product_id": uuid.uuid4(),
                "external_id": f"VEXT-{i}",
                "sku": None,
                "barcode": None,
                "title": "Variant",
                "option_values": None,
                "current_price": Decimal("1.00"),
                "currency": "USD",
                "url": None,
                "status": "active",
            }
        )
    statements = plan_upsert(rows, is_variant=True)
    assert len(statements) <= 3


def test_plan_upsert_dedups_before_building_statements() -> None:
    rows = [
        {
            "workspace_id": WS,
            "external_id": "DUP",
            "sku": None,
            "title": "first",
            "brand": None,
            "barcode": None,
            "url": None,
            "status": "active",
        },
        {
            "workspace_id": WS,
            "external_id": "DUP",
            "sku": None,
            "title": "second",
            "brand": None,
            "barcode": None,
            "url": None,
            "status": "active",
        },
    ]
    statements = plan_upsert(rows, is_variant=False)
    assert len(statements) == 1
    sql = _compiled(statements[0])
    # Only one VALUES row survives (last-wins) -- one insert placeholder set.
    assert sql.count("INSERT INTO products") == 1


def test_plan_upsert_empty_batch_is_zero_statements() -> None:
    assert plan_upsert([], is_variant=False) == []
    assert plan_upsert([], is_variant=True) == []


# --- variant parent resolution -----------------------------------------------


def test_variant_parent_lookup_keys_skips_rows_with_explicit_product_id() -> None:
    rows = [
        {"product_id": uuid.uuid4(), "product_external_id": "PEXT-1"},
        {"product_external_id": "PEXT-2"},
        {"product_sku": "PSKU-1"},
    ]
    external_ids, skus = variant_parent_lookup_keys(rows)
    assert external_ids == {"PEXT-2"}
    assert skus == {"PSKU-1"}


def test_resolve_variant_parents_fills_product_id_and_reports_unresolved() -> None:
    known_pid = uuid.uuid4()
    rows = [
        {"product_external_id": "PEXT-1", "title": "Red"},
        {"product_sku": "PSKU-X", "title": "Blue"},  # unresolvable
    ]
    resolved, unresolved = resolve_variant_parents(
        rows, by_external_id={"PEXT-1": known_pid}, by_sku={}
    )
    assert len(resolved) == 1
    assert resolved[0]["product_id"] == known_pid
    assert len(unresolved) == 1
    assert unresolved[0]["product_sku"] == "PSKU-X"


def test_resolve_variant_parents_passes_through_explicit_product_id() -> None:
    pid = uuid.uuid4()
    rows = [{"product_id": pid, "title": "Red"}]
    resolved, unresolved = resolve_variant_parents(rows, by_external_id={}, by_sku={})
    assert unresolved == []
    assert resolved[0]["product_id"] == pid


# --- default-variant injection (FR-012 tail) ----------------------------------


def test_inject_default_variants_only_for_zero_variant_products() -> None:
    products = [
        {"title": "A", "price": Decimal("1.00"), "currency": "USD", "variants": None},
        {
            "title": "B",
            "price": Decimal("2.00"),
            "currency": "USD",
            "variants": [{"title": "Explicit"}],
        },
    ]
    ids = [uuid.uuid4(), uuid.uuid4()]
    extra = inject_default_variants(products, ids)
    assert len(extra) == 1
    assert extra[0]["product_id"] == ids[0]
    assert extra[0]["title"] == "A"
