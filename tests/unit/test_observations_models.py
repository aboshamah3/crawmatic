"""Observation/current-price ORM model shape tests (SPEC-07 T013, FR-012, §22).

Pure ORM/metadata assertions — no database. Verifies the three SPEC-07
tables (`price_observations`, `request_attempts`, `match_current_prices`)
match `data-model.md` / `contracts/models-observations.md` exactly:
column shapes/nullability, the composite PK (incl. the partition key) on
both partitioned tables, the `postgresql_partition_by` table option,
`unique(workspace_id, match_id)` on `match_current_prices`,
`Money`->`NUMERIC(18,4)` / `NUMERIC(5,4)` / `CHAR(3)` column types, and
enum columns rendering `VARCHAR(32)`.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.models.base import WorkspaceScopedBase
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)}


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)}


# --- PriceObservation -------------------------------------------------------


def test_price_observation_table_name_and_columns() -> None:
    table = PriceObservation.__table__
    assert table.name == "price_observations"
    expected_columns = {
        "id",
        "scraped_at",
        "workspace_id",
        "match_id",
        "product_id",
        "product_variant_id",
        "scrape_job_id",
        "price",
        "old_price",
        "currency",
        "stock_status",
        "raw_title",
        "success",
        "comparable",
        "error_code",
        "error_message",
        "extraction_method",
        "extraction_confidence",
        "selector_used",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_price_observation_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in PriceObservation.__mro__
    assert PriceObservation.__table__.c.workspace_id.nullable is False


def test_price_observation_composite_pk_includes_partition_key() -> None:
    table = PriceObservation.__table__
    pk_columns = set(table.primary_key.columns.keys())
    assert pk_columns == {"id", "scraped_at"}
    assert table.c.scraped_at.primary_key is True


def test_price_observation_is_partitioned_by_scraped_at() -> None:
    table = PriceObservation.__table__
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (scraped_at)"


def test_price_observation_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(PriceObservation.__table__)
    assert "fk_price_observations_workspace_id_workspaces" in fks


def test_price_observation_soft_refs_have_no_fk() -> None:
    table = PriceObservation.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    for col in ("match_id", "product_id", "product_variant_id", "scrape_job_id"):
        assert col not in referencing_cols, col


def test_price_observation_success_and_comparable_not_nullable() -> None:
    table = PriceObservation.__table__
    assert table.c.success.nullable is False
    assert table.c.comparable.nullable is False


def test_price_observation_price_is_money_numeric_18_4() -> None:
    table = PriceObservation.__table__
    assert table.c.price.nullable is True
    assert "NUMERIC(18, 4)" in _compiled_type(table.c.price).upper()
    assert "NUMERIC(18, 4)" in _compiled_type(table.c.old_price).upper()


def test_price_observation_currency_is_char_3() -> None:
    table = PriceObservation.__table__
    assert "CHAR(3)" in _compiled_type(table.c.currency).upper()


def test_price_observation_extraction_confidence_is_numeric_5_4() -> None:
    table = PriceObservation.__table__
    assert "NUMERIC(5, 4)" in _compiled_type(table.c.extraction_confidence).upper()


def test_price_observation_enum_columns_render_varchar_32() -> None:
    table = PriceObservation.__table__
    assert _compiled_type(table.c.stock_status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.error_code).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.extraction_method).upper() == "VARCHAR(32)"


# --- RequestAttempt ----------------------------------------------------------


def test_request_attempt_table_name_and_columns() -> None:
    table = RequestAttempt.__table__
    assert table.name == "request_attempts"
    expected_columns = {
        "id",
        "created_at",
        "workspace_id",
        "scrape_job_id",
        "match_id",
        "attempt_number",
        "url",
        "access_method",
        "proxy_provider_id",
        "proxy_country",
        "status_code",
        "response_time_ms",
        "success",
        "error_code",
        "error_message",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_request_attempt_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in RequestAttempt.__mro__
    assert RequestAttempt.__table__.c.workspace_id.nullable is False


def test_request_attempt_composite_pk_includes_partition_key() -> None:
    table = RequestAttempt.__table__
    pk_columns = set(table.primary_key.columns.keys())
    assert pk_columns == {"id", "created_at"}
    assert table.c.created_at.primary_key is True


def test_request_attempt_is_partitioned_by_created_at() -> None:
    table = RequestAttempt.__table__
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"


def test_request_attempt_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(RequestAttempt.__table__)
    assert "fk_request_attempts_workspace_id_workspaces" in fks


def test_request_attempt_soft_refs_have_no_fk() -> None:
    table = RequestAttempt.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    for col in ("match_id", "scrape_job_id", "proxy_provider_id"):
        assert col not in referencing_cols, col


def test_request_attempt_required_columns_not_nullable() -> None:
    table = RequestAttempt.__table__
    for col in ("match_id", "attempt_number", "url", "access_method", "success"):
        assert table.c[col].nullable is False, col


def test_request_attempt_access_method_enum_renders_varchar_32() -> None:
    table = RequestAttempt.__table__
    assert _compiled_type(table.c.access_method).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.error_code).upper() == "VARCHAR(32)"


# --- MatchCurrentPrice --------------------------------------------------------


def test_match_current_price_table_name_and_columns() -> None:
    table = MatchCurrentPrice.__table__
    assert table.name == "match_current_prices"
    expected_columns = {
        "id",
        "workspace_id",
        "match_id",
        "product_id",
        "product_variant_id",
        "competitor_id",
        "price",
        "old_price",
        "currency",
        "stock_status",
        "comparable",
        "observation_id",
        "success",
        "error_code",
        "extraction_method",
        "extraction_confidence",
        "scraped_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_match_current_price_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in MatchCurrentPrice.__mro__
    assert MatchCurrentPrice.__table__.c.workspace_id.nullable is False


def test_match_current_price_single_column_pk() -> None:
    table = MatchCurrentPrice.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_match_current_price_is_not_partitioned() -> None:
    table = MatchCurrentPrice.__table__
    assert table.dialect_options["postgresql"]["partition_by"] is None


def test_match_current_price_has_unique_workspace_id_match_id() -> None:
    uniques = _unique_constraints(MatchCurrentPrice.__table__)
    key = "uq_match_current_prices_workspace_id_match_id"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"workspace_id", "match_id"}


def test_match_current_price_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(MatchCurrentPrice.__table__)
    assert "fk_match_current_prices_workspace_id_workspaces" in fks


def test_match_current_price_observation_id_has_no_fk() -> None:
    table = MatchCurrentPrice.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    assert "observation_id" not in referencing_cols
    assert table.c.observation_id.nullable is True


def test_match_current_price_price_is_money_numeric_18_4() -> None:
    table = MatchCurrentPrice.__table__
    assert "NUMERIC(18, 4)" in _compiled_type(table.c.price).upper()


def test_match_current_price_currency_is_char_3() -> None:
    table = MatchCurrentPrice.__table__
    assert "CHAR(3)" in _compiled_type(table.c.currency).upper()


def test_match_current_price_extraction_confidence_is_numeric_5_4() -> None:
    table = MatchCurrentPrice.__table__
    assert "NUMERIC(5, 4)" in _compiled_type(table.c.extraction_confidence).upper()


def test_match_current_price_enum_columns_render_varchar_32() -> None:
    table = MatchCurrentPrice.__table__
    assert _compiled_type(table.c.stock_status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.error_code).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.extraction_method).upper() == "VARCHAR(32)"


def test_match_current_price_success_comparable_not_nullable() -> None:
    table = MatchCurrentPrice.__table__
    assert table.c.success.nullable is False
    assert table.c.comparable.nullable is False


def test_match_current_price_scraped_at_not_nullable() -> None:
    table = MatchCurrentPrice.__table__
    assert table.c.scraped_at.nullable is False
