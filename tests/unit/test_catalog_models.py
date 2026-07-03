"""Catalog ORM model shape tests (SPEC-04 T009, FR-004/FR-008/FR-009/SC-008).

Pure ORM/metadata assertions — no database. Verifies the four catalog
tables (`products`, `product_variants`, `product_groups`,
`product_group_items`) match `data-model.md` / `contracts/models-catalog.md`
exactly: column shapes/nullability, `ProductGroupItem` has no
`updated_at`, partial-unique `postgresql_where` renders `... IS NOT
NULL`, full uniques, `unique(workspace_id, id)` parents, composite-FK
shape + naming-convention render, enum columns render `VARCHAR(32)`,
and the `Money` column renders `NUMERIC(18, 4)`.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.enums import GroupStatus, ProductStatus, VariantStatus
from app_shared.models.base import WorkspaceScopedBase
from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _partial_unique_indexes(table) -> dict[str, Index]:
    return {ix.name: ix for ix in table.indexes if ix.unique}


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {
        uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)
    }


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {
        fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)
    }


# --- Product ------------------------------------------------------------


def test_product_table_name_and_columns() -> None:
    table = Product.__table__
    assert table.name == "products"
    expected_columns = {
        "id",
        "workspace_id",
        "external_id",
        "sku",
        "title",
        "brand",
        "barcode",
        "url",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_product_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in Product.__mro__
    assert Product.__table__.c.workspace_id.nullable is False


def test_product_title_required_no_price_column() -> None:
    table = Product.__table__
    assert table.c.title.nullable is False
    assert "price" not in table.c.keys()
    assert "current_price" not in table.c.keys()


def test_product_has_unique_workspace_id_id() -> None:
    uniques = _unique_constraints(Product.__table__)
    assert "uq_products_workspace_id_id" in uniques
    assert set(uniques["uq_products_workspace_id_id"].columns.keys()) == {
        "workspace_id",
        "id",
    }


def test_product_partial_unique_indexes_render_is_not_null() -> None:
    indexes = _partial_unique_indexes(Product.__table__)
    assert "uq_products_workspace_id_external_id" in indexes
    assert "uq_products_workspace_id_sku" in indexes

    ext_ix = indexes["uq_products_workspace_id_external_id"]
    sku_ix = indexes["uq_products_workspace_id_sku"]
    assert str(ext_ix.dialect_options["postgresql"]["where"]) == "external_id IS NOT NULL"
    assert str(sku_ix.dialect_options["postgresql"]["where"]) == "sku IS NOT NULL"
    assert set(ext_ix.columns.keys()) == {"workspace_id", "external_id"}
    assert set(sku_ix.columns.keys()) == {"workspace_id", "sku"}


def test_product_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(Product.__table__)
    assert "fk_products_workspace_id_workspaces" in fks


# --- ProductVariant -------------------------------------------------------


def test_product_variant_table_name_and_columns() -> None:
    table = ProductVariant.__table__
    assert table.name == "product_variants"
    expected_columns = {
        "id",
        "workspace_id",
        "product_id",
        "external_id",
        "sku",
        "barcode",
        "title",
        "option_values",
        "current_price",
        "currency",
        "url",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_product_variant_money_and_currency_not_null() -> None:
    table = ProductVariant.__table__
    assert table.c.current_price.nullable is False
    assert table.c.currency.nullable is False
    assert "NUMERIC(18, 4)" in _compiled_type(table.c.current_price).upper()
    assert "CHAR(3)" in _compiled_type(table.c.currency).upper()


def test_product_variant_option_values_is_jsonb_nullable() -> None:
    table = ProductVariant.__table__
    assert table.c.option_values.nullable is True
    assert "JSONB" in _compiled_type(table.c.option_values).upper()


def test_product_variant_has_unique_workspace_id_id() -> None:
    uniques = _unique_constraints(ProductVariant.__table__)
    assert "uq_product_variants_workspace_id_id" in uniques


def test_product_variant_full_unique_workspace_product_title() -> None:
    uniques = _unique_constraints(ProductVariant.__table__)
    key = "uq_product_variants_workspace_id_product_id_title"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"workspace_id", "product_id", "title"}


def test_product_variant_partial_unique_indexes_render_is_not_null() -> None:
    indexes = _partial_unique_indexes(ProductVariant.__table__)
    assert "uq_product_variants_workspace_id_external_id" in indexes
    assert "uq_product_variants_workspace_id_sku" in indexes
    ext_ix = indexes["uq_product_variants_workspace_id_external_id"]
    assert str(ext_ix.dialect_options["postgresql"]["where"]) == "external_id IS NOT NULL"


def test_product_variant_composite_fk_to_products() -> None:
    fks = _fk_constraints(ProductVariant.__table__)
    key = "fk_product_variants_workspace_id_product_id_products"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["workspace_id", "product_id"]
    assert [e.column.name for e in fk.elements] == ["workspace_id", "id"]
    assert all(e.column.table.name == "products" for e in fk.elements)


def test_product_variant_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ProductVariant.__table__)
    assert "fk_product_variants_workspace_id_workspaces" in fks


# --- ProductGroup -----------------------------------------------------


def test_product_group_table_name_and_columns() -> None:
    table = ProductGroup.__table__
    assert table.name == "product_groups"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "description",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_product_group_has_unique_workspace_id_id_and_name() -> None:
    uniques = _unique_constraints(ProductGroup.__table__)
    assert "uq_product_groups_workspace_id_id" in uniques
    assert "uq_product_groups_workspace_id_name" in uniques


def test_product_group_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ProductGroup.__table__)
    assert "fk_product_groups_workspace_id_workspaces" in fks


# --- ProductGroupItem ---------------------------------------------------


def test_product_group_item_table_name_and_columns() -> None:
    table = ProductGroupItem.__table__
    assert table.name == "product_group_items"
    expected_columns = {
        "id",
        "workspace_id",
        "product_group_id",
        "product_id",
        "product_variant_id",
        "created_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_product_group_item_has_no_updated_at() -> None:
    assert "updated_at" not in ProductGroupItem.__table__.c.keys()


def test_product_group_item_product_and_variant_refs_are_nullable() -> None:
    table = ProductGroupItem.__table__
    assert table.c.product_id.nullable is True
    assert table.c.product_variant_id.nullable is True
    assert table.c.product_group_id.nullable is False


def test_product_group_item_partial_unique_membership_indexes() -> None:
    indexes = _partial_unique_indexes(ProductGroupItem.__table__)
    product_key = "uq_product_group_items_workspace_id_group_id_product_id"
    variant_key = "uq_product_group_items_workspace_id_group_id_variant_id"
    assert product_key in indexes
    assert variant_key in indexes
    assert (
        str(indexes[product_key].dialect_options["postgresql"]["where"])
        == "product_id IS NOT NULL"
    )
    assert (
        str(indexes[variant_key].dialect_options["postgresql"]["where"])
        == "product_variant_id IS NOT NULL"
    )


def test_product_group_item_composite_fks() -> None:
    fks = _fk_constraints(ProductGroupItem.__table__)
    group_key = "fk_product_group_items_workspace_id_group_id_product_groups"
    product_key = "fk_product_group_items_workspace_id_product_id_products"
    variant_key = (
        "fk_product_group_items_workspace_id_variant_id_product_variants"
    )
    assert group_key in fks
    assert product_key in fks
    assert variant_key in fks

    assert [c.name for c in fks[group_key].columns] == ["workspace_id", "product_group_id"]
    assert all(e.column.table.name == "product_groups" for e in fks[group_key].elements)

    assert [c.name for c in fks[product_key].columns] == ["workspace_id", "product_id"]
    assert all(e.column.table.name == "products" for e in fks[product_key].elements)

    assert [c.name for c in fks[variant_key].columns] == [
        "workspace_id",
        "product_variant_id",
    ]
    assert all(
        e.column.table.name == "product_variants" for e in fks[variant_key].elements
    )


def test_product_group_item_nullable_composite_fks_use_match_simple() -> None:
    fks = _fk_constraints(ProductGroupItem.__table__)
    product_key = "fk_product_group_items_workspace_id_product_id_products"
    variant_key = (
        "fk_product_group_items_workspace_id_variant_id_product_variants"
    )
    assert fks[product_key].match == "SIMPLE"
    assert fks[variant_key].match == "SIMPLE"


def test_product_group_item_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ProductGroupItem.__table__)
    assert "fk_product_group_items_workspace_id_workspaces" in fks


# --- Enum columns render as plain VARCHAR(32) ---------------------------


def test_status_enum_columns_render_as_varchar_32() -> None:
    assert _compiled_type(Product.__table__.c.status).upper() == "VARCHAR(32)"
    assert _compiled_type(ProductVariant.__table__.c.status).upper() == "VARCHAR(32)"
    assert _compiled_type(ProductGroup.__table__.c.status).upper() == "VARCHAR(32)"


def test_status_enum_columns_coerce_on_bind() -> None:
    assert (
        Product.__table__.c.status.type.process_bind_param(ProductStatus.ARCHIVED, _PG_DIALECT)
        == "archived"
    )
    assert (
        ProductVariant.__table__.c.status.type.process_bind_param(
            VariantStatus.ACTIVE, _PG_DIALECT
        )
        == "active"
    )
    assert (
        ProductGroup.__table__.c.status.type.process_bind_param(
            GroupStatus.ARCHIVED, _PG_DIALECT
        )
        == "archived"
    )


def test_product_group_item_has_no_status_column() -> None:
    assert "status" not in ProductGroupItem.__table__.c.keys()
