"""Competitor/match ORM model shape tests (SPEC-05 T007, FR-004/FR-005/FR-006/FR-017, SC-002).

Pure ORM/metadata assertions — no database. Verifies the two SPEC-05
tables (`competitors`, `competitor_product_matches`) match
`data-model.md` / `contracts/models-competitors-matches.md` exactly:
column shapes/nullability, `unique(workspace_id, domain)` +
`unique(workspace_id, id)` on competitors, the 4-column match unique,
the three composite workspace-local FKs + their explicit `cpm` names,
**every** emitted constraint/index name is <=63 bytes (research D5),
enum columns render `VARCHAR(32)`, health-field defaults, and that
`current_price_id`/`scrape_profile_id`/`access_policy_id` carry no FK.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.enums import (
    CompetitorStatus,
    HealthStatus,
    LegalStatus,
    MatchPriority,
    MatchStatus,
    RobotsPolicy,
)
from app_shared.models.base import WorkspaceScopedBase
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)}


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


# --- Competitor -----------------------------------------------------------


def test_competitor_table_name_and_columns() -> None:
    table = Competitor.__table__
    assert table.name == "competitors"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "domain",
        "status",
        "legal_status",
        "robots_policy",
        "default_scrape_profile_id",
        "default_access_policy_id",
        "max_concurrent_requests",
        "max_requests_per_minute",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_competitor_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in Competitor.__mro__
    assert Competitor.__table__.c.workspace_id.nullable is False


def test_competitor_name_and_domain_required() -> None:
    table = Competitor.__table__
    assert table.c.name.nullable is False
    assert table.c.domain.nullable is False


def test_competitor_has_unique_workspace_id_id_and_domain() -> None:
    uniques = _unique_constraints(Competitor.__table__)
    assert "uq_competitors_workspace_id_id" in uniques
    assert set(uniques["uq_competitors_workspace_id_id"].columns.keys()) == {
        "workspace_id",
        "id",
    }
    assert "uq_competitors_workspace_id_domain" in uniques
    assert set(uniques["uq_competitors_workspace_id_domain"].columns.keys()) == {
        "workspace_id",
        "domain",
    }


def test_competitor_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(Competitor.__table__)
    assert "fk_competitors_workspace_id_workspaces" in fks


def test_competitor_optional_profile_policy_and_caps_nullable() -> None:
    table = Competitor.__table__
    assert table.c.default_scrape_profile_id.nullable is True
    assert table.c.default_access_policy_id.nullable is True
    assert table.c.max_concurrent_requests.nullable is True
    assert table.c.max_requests_per_minute.nullable is True


def test_competitor_default_access_policy_id_has_no_fk() -> None:
    # access_policies lands in a later spec (SPEC-10) — still no FK.
    fks = _fk_constraints(Competitor.__table__)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    assert "default_access_policy_id" not in referencing_cols


def test_competitor_default_scrape_profile_id_fk_on_delete_set_null() -> None:
    # SPEC-06 promotes this column to a plain FK -> scrape_profiles(id)
    # ON DELETE SET NULL (research D5, FR-012/FR-023).
    fks = _fk_constraints(Competitor.__table__)
    key = "fk_competitors_default_scrape_profile_id_scrape_profiles"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["default_scrape_profile_id"]
    assert all(e.column.table.name == "scrape_profiles" for e in fk.elements)
    assert fk.ondelete == "SET NULL"


def test_competitor_status_enum_defaults() -> None:
    table = Competitor.__table__
    assert table.c.status.default.arg == CompetitorStatus.ACTIVE
    assert table.c.legal_status.default.arg == LegalStatus.REVIEW_REQUIRED
    assert table.c.robots_policy.default.arg == RobotsPolicy.RESPECT


def test_competitor_enum_columns_render_varchar_32() -> None:
    table = Competitor.__table__
    assert _compiled_type(table.c.status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.legal_status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.robots_policy).upper() == "VARCHAR(32)"


# --- CompetitorProductMatch -------------------------------------------------


def test_match_table_name_and_columns() -> None:
    table = CompetitorProductMatch.__table__
    assert table.name == "competitor_product_matches"
    expected_columns = {
        "id",
        "workspace_id",
        "product_id",
        "product_variant_id",
        "competitor_id",
        "competitor_url",
        "normalized_competitor_url",
        "url_pattern",
        "url_pattern_version",
        "competitor_variant_identifier",
        "competitor_variant_sku",
        "competitor_variant_options",
        "external_title",
        "scrape_profile_id",
        "access_policy_id",
        "priority",
        "status",
        "health_status",
        "last_error_code",
        "consecutive_failures",
        "success_rate_7d",
        "current_price_id",
        "last_scraped_at",
        "last_success_at",
        "last_failed_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_match_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in CompetitorProductMatch.__mro__
    assert CompetitorProductMatch.__table__.c.workspace_id.nullable is False


def test_match_required_ref_and_url_columns_not_nullable() -> None:
    table = CompetitorProductMatch.__table__
    for col in (
        "product_id",
        "product_variant_id",
        "competitor_id",
        "competitor_url",
        "normalized_competitor_url",
        "url_pattern",
        "url_pattern_version",
    ):
        assert table.c[col].nullable is False, col


def test_match_url_pattern_version_and_consecutive_failures_are_integer() -> None:
    table = CompetitorProductMatch.__table__
    assert "INTEGER" in _compiled_type(table.c.url_pattern_version).upper()
    assert "INTEGER" in _compiled_type(table.c.consecutive_failures).upper()


def test_match_success_rate_7d_is_numeric_5_4_and_nullable() -> None:
    table = CompetitorProductMatch.__table__
    assert table.c.success_rate_7d.nullable is True
    assert "NUMERIC(5, 4)" in _compiled_type(table.c.success_rate_7d).upper()


def test_match_competitor_variant_options_is_jsonb_nullable() -> None:
    table = CompetitorProductMatch.__table__
    assert table.c.competitor_variant_options.nullable is True
    assert "JSONB" in _compiled_type(table.c.competitor_variant_options).upper()


def test_match_has_4_col_unique_with_explicit_name() -> None:
    uniques = _unique_constraints(CompetitorProductMatch.__table__)
    key = "uq_cpm_ws_variant_competitor_norm_url"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {
        "workspace_id",
        "product_variant_id",
        "competitor_id",
        "normalized_competitor_url",
    }


def test_match_composite_fk_to_products() -> None:
    fks = _fk_constraints(CompetitorProductMatch.__table__)
    key = "fk_cpm_workspace_product_products"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["workspace_id", "product_id"]
    assert [e.column.name for e in fk.elements] == ["workspace_id", "id"]
    assert all(e.column.table.name == "products" for e in fk.elements)


def test_match_composite_fk_to_product_variants() -> None:
    fks = _fk_constraints(CompetitorProductMatch.__table__)
    key = "fk_cpm_workspace_variant_variants"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["workspace_id", "product_variant_id"]
    assert all(e.column.table.name == "product_variants" for e in fk.elements)


def test_match_composite_fk_to_competitors() -> None:
    fks = _fk_constraints(CompetitorProductMatch.__table__)
    key = "fk_cpm_workspace_competitor_competitors"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["workspace_id", "competitor_id"]
    assert all(e.column.table.name == "competitors" for e in fk.elements)


def test_match_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(CompetitorProductMatch.__table__)
    assert "fk_cpm_workspace_workspaces" in fks


def test_match_current_price_and_access_policy_have_no_fk() -> None:
    # current_price_id/access_policy_id: soft/deferred references, targets
    # SPEC-09/10 don't exist yet — still no FK.
    table = CompetitorProductMatch.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    assert "current_price_id" not in referencing_cols
    assert "access_policy_id" not in referencing_cols
    assert table.c.current_price_id.nullable is True
    assert table.c.access_policy_id.nullable is True


def test_match_scrape_profile_id_fk_on_delete_set_null() -> None:
    # SPEC-06 promotes this column to a plain FK -> scrape_profiles(id)
    # ON DELETE SET NULL (research D5, FR-012/FR-023).
    table = CompetitorProductMatch.__table__
    fks = _fk_constraints(table)
    key = "fk_cpm_scrape_profile_id_scrape_profiles"
    assert key in fks
    fk = fks[key]
    assert [c.name for c in fk.columns] == ["scrape_profile_id"]
    assert all(e.column.table.name == "scrape_profiles" for e in fk.elements)
    assert fk.ondelete == "SET NULL"
    assert table.c.scrape_profile_id.nullable is True


def test_match_enum_columns_render_varchar_32() -> None:
    table = CompetitorProductMatch.__table__
    assert _compiled_type(table.c.priority).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.health_status).upper() == "VARCHAR(32)"


def test_match_priority_status_health_defaults() -> None:
    table = CompetitorProductMatch.__table__
    assert table.c.priority.default.arg == MatchPriority.NORMAL
    assert table.c.status.default.arg == MatchStatus.ACTIVE
    assert table.c.health_status.default.arg == HealthStatus.UNKNOWN
    assert table.c.consecutive_failures.default.arg == 0


def test_match_health_fields_nullable_and_default_null() -> None:
    table = CompetitorProductMatch.__table__
    for col in (
        "success_rate_7d",
        "current_price_id",
        "last_error_code",
        "last_scraped_at",
        "last_success_at",
        "last_failed_at",
    ):
        assert table.c[col].nullable is True, col
        assert table.c[col].default is None, col


# --- Constraint/index name length budget (research D5) ---------------------


def test_every_competitor_constraint_and_index_name_fits_63_bytes() -> None:
    for name in _all_constraint_and_index_names(Competitor.__table__):
        assert len(name.encode("utf-8")) <= 63, name


def test_every_match_constraint_and_index_name_fits_63_bytes() -> None:
    names = _all_constraint_and_index_names(CompetitorProductMatch.__table__)
    # Sanity: the four explicit `cpm` names + the pk + the auto index must
    # all be present and all within budget.
    expected_present = {
        "uq_cpm_ws_variant_competitor_norm_url",
        "fk_cpm_workspace_product_products",
        "fk_cpm_workspace_variant_variants",
        "fk_cpm_workspace_competitor_competitors",
        "fk_cpm_workspace_workspaces",
        "pk_competitor_product_matches",
        "ix_competitor_product_matches_workspace_id",
    }
    assert expected_present.issubset(set(names))
    for name in names:
        assert len(name.encode("utf-8")) <= 63, name
