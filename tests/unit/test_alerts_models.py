"""Alert/price-comparison ORM model shape tests (SPEC-09 T010, FR-001/FR-002/FR-003).

Pure ORM/metadata assertions — no database. Verifies the three SPEC-09
tables (`variant_price_states`, `variant_alert_states`,
`price_alert_events`) match `data-model.md` /
`contracts/models-alerts.md` exactly: column shapes/nullability,
`unique(workspace_id, product_variant_id)` on both current-state
tables, the composite PK (incl. the partition key) + `postgresql_
partition_by` on `price_alert_events`, `Money`->`NUMERIC(18,4)` /
`CHAR(3)` / `JSONB` column types, enum columns rendering `VARCHAR(32)`,
registration in `WORKSPACE_OWNED_MODELS` + re-export from
`app_shared.models`, and constraint/index name length (<=63 bytes).
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.models import PriceAlertEvent, VariantAlertState, VariantPriceState
from app_shared.models.base import TimestampMixin, WorkspaceScopedBase
from app_shared.repository import WORKSPACE_OWNED_MODELS

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)}


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)}


def _all_constraint_and_index_names(table) -> list[str]:
    names = [c.name for c in table.constraints if c.name is not None]
    names.extend(ix.name for ix in table.indexes if ix.name is not None)
    return names


# --- VariantPriceState -------------------------------------------------------


def test_variant_price_state_table_name_and_columns() -> None:
    table = VariantPriceState.__table__
    assert table.name == "variant_price_states"
    expected_columns = {
        "id",
        "workspace_id",
        "product_id",
        "product_variant_id",
        "client_price",
        "currency",
        "cheapest_competitor_price",
        "average_competitor_price",
        "highest_competitor_price",
        "comparable_competitor_count",
        "latest_alert_type",
        "latest_alert_severity",
        "latest_alert_state_id",
        "calculated_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_variant_price_state_uses_workspace_scoped_base_and_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in VariantPriceState.__mro__
    assert TimestampMixin in VariantPriceState.__mro__
    assert VariantPriceState.__table__.c.workspace_id.nullable is False


def test_variant_price_state_single_column_pk() -> None:
    table = VariantPriceState.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_variant_price_state_has_unique_workspace_id_product_variant_id() -> None:
    uniques = _unique_constraints(VariantPriceState.__table__)
    key = "uq_variant_price_states_workspace_id_product_variant_id"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"workspace_id", "product_variant_id"}


def test_variant_price_state_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(VariantPriceState.__table__)
    assert "fk_variant_price_states_workspace_id_workspaces" in fks


def test_variant_price_state_money_columns_are_numeric_18_4() -> None:
    table = VariantPriceState.__table__
    for col in (
        "client_price",
        "cheapest_competitor_price",
        "average_competitor_price",
        "highest_competitor_price",
    ):
        assert "NUMERIC(18, 4)" in _compiled_type(table.c[col]).upper(), col


def test_variant_price_state_currency_is_char_3() -> None:
    table = VariantPriceState.__table__
    assert "CHAR(3)" in _compiled_type(table.c.currency).upper()


def test_variant_price_state_required_columns_not_nullable() -> None:
    table = VariantPriceState.__table__
    for col in (
        "product_id",
        "product_variant_id",
        "client_price",
        "currency",
        "comparable_competitor_count",
        "latest_alert_type",
        "latest_alert_severity",
        "calculated_at",
    ):
        assert table.c[col].nullable is False, col


def test_variant_price_state_nullable_benchmarks_and_link() -> None:
    table = VariantPriceState.__table__
    for col in (
        "cheapest_competitor_price",
        "average_competitor_price",
        "highest_competitor_price",
        "latest_alert_state_id",
    ):
        assert table.c[col].nullable is True, col


def test_variant_price_state_enum_columns_render_varchar_32() -> None:
    table = VariantPriceState.__table__
    assert _compiled_type(table.c.latest_alert_type).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.latest_alert_severity).upper() == "VARCHAR(32)"


# --- VariantAlertState --------------------------------------------------------


def test_variant_alert_state_table_name_and_columns() -> None:
    table = VariantAlertState.__table__
    assert table.name == "variant_alert_states"
    expected_columns = {
        "id",
        "workspace_id",
        "product_id",
        "product_variant_id",
        "type",
        "severity",
        "status",
        "client_price",
        "benchmark_price",
        "cheapest_competitor_price",
        "average_competitor_price",
        "message",
        "details",
        "first_seen_at",
        "last_seen_at",
        "resolved_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_variant_alert_state_uses_workspace_scoped_base_and_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in VariantAlertState.__mro__
    assert TimestampMixin in VariantAlertState.__mro__
    assert VariantAlertState.__table__.c.workspace_id.nullable is False


def test_variant_alert_state_single_column_pk() -> None:
    table = VariantAlertState.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_variant_alert_state_has_unique_workspace_id_product_variant_id() -> None:
    uniques = _unique_constraints(VariantAlertState.__table__)
    key = "uq_variant_alert_states_workspace_id_product_variant_id"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"workspace_id", "product_variant_id"}


def test_variant_alert_state_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(VariantAlertState.__table__)
    assert "fk_variant_alert_states_workspace_id_workspaces" in fks


def test_variant_alert_state_enum_columns_render_varchar_32() -> None:
    table = VariantAlertState.__table__
    assert _compiled_type(table.c.type).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.severity).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.status).upper() == "VARCHAR(32)"


def test_variant_alert_state_money_columns_are_numeric_18_4() -> None:
    table = VariantAlertState.__table__
    for col in (
        "client_price",
        "benchmark_price",
        "cheapest_competitor_price",
        "average_competitor_price",
    ):
        assert "NUMERIC(18, 4)" in _compiled_type(table.c[col]).upper(), col


def test_variant_alert_state_details_is_jsonb() -> None:
    table = VariantAlertState.__table__
    assert isinstance(table.c.details.type, postgresql.JSONB)
    assert table.c.details.nullable is True


def test_variant_alert_state_message_not_nullable() -> None:
    table = VariantAlertState.__table__
    assert table.c.message.nullable is False


def test_variant_alert_state_required_timestamps_not_nullable() -> None:
    table = VariantAlertState.__table__
    assert table.c.first_seen_at.nullable is False
    assert table.c.last_seen_at.nullable is False
    assert table.c.resolved_at.nullable is True


# --- PriceAlertEvent (PARTITIONED) --------------------------------------------


def test_price_alert_event_table_name_and_columns() -> None:
    table = PriceAlertEvent.__table__
    assert table.name == "price_alert_events"
    expected_columns = {
        "id",
        "created_at",
        "workspace_id",
        "product_id",
        "product_variant_id",
        "alert_state_id",
        "event_type",
        "previous_type",
        "new_type",
        "previous_severity",
        "new_severity",
        "message",
        "details",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_price_alert_event_uses_workspace_scoped_base_no_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in PriceAlertEvent.__mro__
    assert TimestampMixin not in PriceAlertEvent.__mro__
    assert PriceAlertEvent.__table__.c.workspace_id.nullable is False
    assert "updated_at" not in PriceAlertEvent.__table__.c.keys()


def test_price_alert_event_composite_pk_includes_partition_key() -> None:
    table = PriceAlertEvent.__table__
    pk_columns = set(table.primary_key.columns.keys())
    assert pk_columns == {"id", "created_at"}
    assert table.c.created_at.primary_key is True


def test_price_alert_event_is_partitioned_by_created_at() -> None:
    table = PriceAlertEvent.__table__
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"


def test_price_alert_event_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(PriceAlertEvent.__table__)
    assert "fk_price_alert_events_workspace_id_workspaces" in fks


def test_price_alert_event_product_variant_id_is_indexed() -> None:
    table = PriceAlertEvent.__table__
    assert table.c.product_variant_id.index is True


def test_price_alert_event_nullability() -> None:
    table = PriceAlertEvent.__table__
    for col in (
        "product_id",
        "product_variant_id",
        "alert_state_id",
        "event_type",
        "new_type",
        "new_severity",
        "message",
    ):
        assert table.c[col].nullable is False, col
    for col in ("previous_type", "previous_severity", "details"):
        assert table.c[col].nullable is True, col


def test_price_alert_event_enum_columns_render_varchar_32() -> None:
    table = PriceAlertEvent.__table__
    for col in ("event_type", "previous_type", "new_type", "previous_severity", "new_severity"):
        assert _compiled_type(table.c[col]).upper() == "VARCHAR(32)", col


def test_price_alert_event_details_is_jsonb() -> None:
    table = PriceAlertEvent.__table__
    assert isinstance(table.c.details.type, postgresql.JSONB)


# --- Registration + constraint/index name length ------------------------------


def test_all_three_models_registered_as_workspace_owned() -> None:
    for model in (VariantPriceState, VariantAlertState, PriceAlertEvent):
        assert model in WORKSPACE_OWNED_MODELS


def test_all_three_models_reexported_from_app_shared_models() -> None:
    import app_shared.models as models_pkg

    assert models_pkg.VariantPriceState is VariantPriceState
    assert models_pkg.VariantAlertState is VariantAlertState
    assert models_pkg.PriceAlertEvent is PriceAlertEvent
    assert "VariantPriceState" in models_pkg.__all__
    assert "VariantAlertState" in models_pkg.__all__
    assert "PriceAlertEvent" in models_pkg.__all__


def test_all_constraint_and_index_names_are_within_63_bytes() -> None:
    for table in (
        VariantPriceState.__table__,
        VariantAlertState.__table__,
        PriceAlertEvent.__table__,
    ):
        for name in _all_constraint_and_index_names(table):
            assert len(name.encode("utf-8")) <= 63, name
