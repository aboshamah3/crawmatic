"""``ScrapeProfile`` ORM model shape tests (SPEC-06 T012, FR-001/FR-002/FR-003, SC-001).

Pure ORM/metadata assertions — no database. Verifies `scrape_profiles`
matches `data-model.md` / `contracts/models-scrape-profiles.md` exactly:
column set/types/nullability, **nullable** `workspace_id` + FK, both
partial unique indexes with their exact `postgresql_where` predicates,
the explicit `ix_scrape_profiles_workspace_id`, documented defaults,
enum columns rendering `VARCHAR(32)`, and every emitted constraint/
index name <=63 bytes.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, Index
from sqlalchemy.dialects import postgresql

from app_shared.enums import AdapterKey, ScrapeProfileMode, VariantStrategy
from app_shared.models.base import Base, TimestampMixin, WorkspaceScopedBase
from app_shared.models.scrape_profiles import ScrapeProfile

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _partial_unique_indexes(table) -> dict[str, Index]:
    return {ix.name: ix for ix in table.indexes if ix.unique}


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)}


def _all_constraint_and_index_names(table) -> list[str]:
    names: list[str] = []
    for c in table.constraints:
        if getattr(c, "name", None):
            names.append(c.name)
    for ix in table.indexes:
        if ix.name:
            names.append(ix.name)
    return names


# --- Class shape ------------------------------------------------------------


def test_scrape_profile_uses_base_and_timestamp_mixin_not_workspace_scoped_base() -> None:
    assert Base in ScrapeProfile.__mro__
    assert TimestampMixin in ScrapeProfile.__mro__
    assert WorkspaceScopedBase not in ScrapeProfile.__mro__


def test_scrape_profile_table_name_and_columns() -> None:
    table = ScrapeProfile.__table__
    assert table.name == "scrape_profiles"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "mode",
        "adapter_key",
        "jsonld_enabled",
        "platform_patterns_enabled",
        "embedded_json_enabled",
        "price_selector",
        "price_xpath",
        "price_regex",
        "old_price_selector",
        "old_price_xpath",
        "old_price_regex",
        "currency_selector",
        "currency_xpath",
        "currency_regex",
        "stock_selector",
        "stock_xpath",
        "stock_regex",
        "title_selector",
        "title_xpath",
        "variant_strategy",
        "variant_selector_config",
        "price_transform_rules",
        "validation_rules",
        "confidence_rules",
        "wait_for_selector",
        "request_timeout_ms",
        "browser_timeout_ms",
        "headers",
        "cookies",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


# --- Dual-scope workspace_id --------------------------------------------------


def test_workspace_id_is_nullable_and_indexed() -> None:
    table = ScrapeProfile.__table__
    assert table.c.workspace_id.nullable is True
    assert table.c.workspace_id.index is True


def test_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ScrapeProfile.__table__)
    assert "fk_scrape_profiles_workspace_id_workspaces" in fks
    fk = fks["fk_scrape_profiles_workspace_id_workspaces"]
    assert [c.name for c in fk.columns] == ["workspace_id"]
    assert all(e.column.table.name == "workspaces" for e in fk.elements)


def test_name_is_not_nullable() -> None:
    assert ScrapeProfile.__table__.c.name.nullable is False


# --- Partial unique indexes ---------------------------------------------------


def test_two_partial_unique_indexes_present_with_exact_where_predicates() -> None:
    indexes = _partial_unique_indexes(ScrapeProfile.__table__)

    tenant = indexes["uq_scrape_profiles_workspace_id_name"]
    assert set(tenant.columns.keys()) == {"workspace_id", "name"}
    assert str(tenant.dialect_options["postgresql"]["where"]) == "workspace_id IS NOT NULL"

    global_ = indexes["uq_scrape_profiles_name_global"]
    assert set(global_.columns.keys()) == {"name"}
    assert str(global_.dialect_options["postgresql"]["where"]) == "workspace_id IS NULL"


def test_explicit_workspace_id_index_present() -> None:
    index_names = {ix.name for ix in ScrapeProfile.__table__.indexes}
    assert "ix_scrape_profiles_workspace_id" in index_names


# --- Documented defaults (FR-002) --------------------------------------------


def test_documented_defaults() -> None:
    table = ScrapeProfile.__table__
    assert table.c.mode.default.arg == ScrapeProfileMode.HTTP
    assert table.c.adapter_key.default.arg == AdapterKey.DEFAULT_HTTP
    assert table.c.jsonld_enabled.default.arg is True
    assert table.c.platform_patterns_enabled.default.arg is True
    assert table.c.embedded_json_enabled.default.arg is True
    assert table.c.variant_strategy.default.arg == VariantStrategy.PAGE_SINGLE_PRICE
    assert table.c.request_timeout_ms.default.arg == 30000


def test_nullable_extraction_and_json_and_timeout_fields() -> None:
    table = ScrapeProfile.__table__
    nullable_fields = (
        "price_selector",
        "price_xpath",
        "price_regex",
        "old_price_selector",
        "old_price_xpath",
        "old_price_regex",
        "currency_selector",
        "currency_xpath",
        "currency_regex",
        "stock_selector",
        "stock_xpath",
        "stock_regex",
        "title_selector",
        "title_xpath",
        "variant_selector_config",
        "price_transform_rules",
        "validation_rules",
        "confidence_rules",
        "wait_for_selector",
        "browser_timeout_ms",
        "headers",
        "cookies",
    )
    for field in nullable_fields:
        assert table.c[field].nullable is True, field
        assert table.c[field].default is None, field


# --- Enum columns render VARCHAR(32) -----------------------------------------


def test_enum_columns_render_varchar_32() -> None:
    table = ScrapeProfile.__table__
    assert _compiled_type(table.c.mode).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.adapter_key).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.variant_strategy).upper() == "VARCHAR(32)"


def test_jsonb_columns_render_jsonb() -> None:
    table = ScrapeProfile.__table__
    for field in (
        "variant_selector_config",
        "price_transform_rules",
        "validation_rules",
        "confidence_rules",
        "headers",
        "cookies",
    ):
        assert "JSONB" in _compiled_type(table.c[field]).upper(), field


def test_request_timeout_ms_is_integer_not_null() -> None:
    table = ScrapeProfile.__table__
    assert "INTEGER" in _compiled_type(table.c.request_timeout_ms).upper()
    assert table.c.request_timeout_ms.nullable is False


# --- Name length budget (research pattern) -----------------------------------


def test_every_constraint_and_index_name_fits_63_bytes() -> None:
    names = _all_constraint_and_index_names(ScrapeProfile.__table__)
    expected_present = {
        "pk_scrape_profiles",
        "fk_scrape_profiles_workspace_id_workspaces",
        "ix_scrape_profiles_workspace_id",
        "uq_scrape_profiles_workspace_id_name",
        "uq_scrape_profiles_name_global",
    }
    assert expected_present.issubset(set(names))
    for name in names:
        assert len(name.encode("utf-8")) <= 63, name
