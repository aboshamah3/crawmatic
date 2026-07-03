"""Jobs ORM model shape tests (SPEC-08 T013, FR-001, FR-002, FR-003).

Pure ORM/metadata assertions — no database. Mirrors
`tests/unit/test_observations_models.py` (SPEC-07) / the SPEC-05
`competitors_matches` precedent: column shapes/nullability,
`enum_column`->`VARCHAR(32)` rendering, `created_at` present /
`updated_at` absent, `unique(scrape_job_id, match_id)` on targets,
`unique(workspace_id, id)` on jobs, the composite target->job FK, the
RLS-anchor FK on both, `match_id` carrying no FK, both models'
`WORKSPACE_OWNED_MODELS` registration + re-export, and every
constraint/index name staying under Postgres's 63-byte identifier cap.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.models.base import WorkspaceScopedBase
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.repository import WORKSPACE_OWNED_MODELS

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)}


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)}


# --- ScrapeJob ---------------------------------------------------------------


def test_scrape_job_table_name_and_columns() -> None:
    table = ScrapeJob.__table__
    assert table.name == "scrape_jobs"
    expected_columns = {
        "id",
        "workspace_id",
        "type",
        "scope",
        "product_id",
        "product_variant_id",
        "product_group_id",
        "competitor_id",
        "match_id",
        "status",
        "priority",
        "total_targets",
        "success_count",
        "failure_count",
        "skipped_count",
        "requested_by",
        "source",
        "started_at",
        "completed_at",
        "created_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))
    assert "updated_at" not in table.c.keys()


def test_scrape_job_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in ScrapeJob.__mro__
    assert ScrapeJob.__table__.c.workspace_id.nullable is False


def test_scrape_job_single_column_pk() -> None:
    table = ScrapeJob.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_scrape_job_has_unique_workspace_id_id() -> None:
    uniques = _unique_constraints(ScrapeJob.__table__)
    key = "uq_scrape_jobs_workspace_id_id"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"workspace_id", "id"}


def test_scrape_job_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ScrapeJob.__table__)
    assert "fk_scrape_jobs_workspace_id_workspaces" in fks


def test_scrape_job_soft_scope_refs_have_no_fk_and_are_nullable() -> None:
    table = ScrapeJob.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    for col in (
        "product_id",
        "product_variant_id",
        "product_group_id",
        "competitor_id",
        "match_id",
        "requested_by",
    ):
        assert col not in referencing_cols, col
        assert table.c[col].nullable is True, col


def test_scrape_job_required_columns_not_nullable() -> None:
    table = ScrapeJob.__table__
    for col in (
        "type",
        "scope",
        "status",
        "priority",
        "total_targets",
        "success_count",
        "failure_count",
        "skipped_count",
        "source",
        "created_at",
    ):
        assert table.c[col].nullable is False, col


def test_scrape_job_lifecycle_timestamps_are_nullable() -> None:
    table = ScrapeJob.__table__
    assert table.c.started_at.nullable is True
    assert table.c.completed_at.nullable is True


def test_scrape_job_enum_columns_render_varchar_32() -> None:
    table = ScrapeJob.__table__
    for col in ("type", "scope", "status", "priority", "source"):
        assert _compiled_type(table.c[col]).upper() == "VARCHAR(32)", col


def test_scrape_job_counters_default_to_zero() -> None:
    table = ScrapeJob.__table__
    for col in ("total_targets", "success_count", "failure_count", "skipped_count"):
        assert table.c[col].default is not None, col
        assert table.c[col].default.arg == 0, col


# --- ScrapeJobTarget -----------------------------------------------------------


def test_scrape_job_target_table_name_and_columns() -> None:
    table = ScrapeJobTarget.__table__
    assert table.name == "scrape_job_targets"
    expected_columns = {
        "id",
        "workspace_id",
        "scrape_job_id",
        "match_id",
        "status",
        "locked_at",
        "started_at",
        "completed_at",
        "error_code",
        "created_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))
    assert "updated_at" not in table.c.keys()


def test_scrape_job_target_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in ScrapeJobTarget.__mro__
    assert ScrapeJobTarget.__table__.c.workspace_id.nullable is False


def test_scrape_job_target_single_column_pk() -> None:
    table = ScrapeJobTarget.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_scrape_job_target_has_unique_scrape_job_id_match_id() -> None:
    uniques = _unique_constraints(ScrapeJobTarget.__table__)
    key = "uq_scrape_job_targets_scrape_job_id_match_id"
    assert key in uniques
    assert set(uniques[key].columns.keys()) == {"scrape_job_id", "match_id"}


def test_scrape_job_target_has_composite_fk_to_scrape_jobs() -> None:
    fks = _fk_constraints(ScrapeJobTarget.__table__)
    key = "fk_scrape_job_targets_workspace_scrape_job_scrape_jobs"
    assert key in fks
    fk = fks[key]
    assert {c.name for c in fk.columns} == {"workspace_id", "scrape_job_id"}
    referred_cols = {elem.target_fullname for elem in fk.elements}
    assert referred_cols == {
        "scrape_jobs.workspace_id",
        "scrape_jobs.id",
    }


def test_scrape_job_target_has_rls_anchor_fk_to_workspaces() -> None:
    fks = _fk_constraints(ScrapeJobTarget.__table__)
    assert "fk_scrape_job_targets_workspace_id_workspaces" in fks


def test_scrape_job_target_match_id_has_no_fk() -> None:
    table = ScrapeJobTarget.__table__
    fks = _fk_constraints(table)
    referencing_cols = {c.name for fk in fks.values() for c in fk.columns}
    assert "match_id" not in referencing_cols
    assert table.c.match_id.nullable is False
    assert table.c.match_id.index is True


def test_scrape_job_target_required_columns_not_nullable() -> None:
    table = ScrapeJobTarget.__table__
    for col in ("scrape_job_id", "match_id", "status", "created_at"):
        assert table.c[col].nullable is False, col


def test_scrape_job_target_lifecycle_and_lock_timestamps_are_nullable() -> None:
    table = ScrapeJobTarget.__table__
    for col in ("locked_at", "started_at", "completed_at", "error_code"):
        assert table.c[col].nullable is True, col


def test_scrape_job_target_enum_columns_render_varchar_32() -> None:
    table = ScrapeJobTarget.__table__
    assert _compiled_type(table.c.status).upper() == "VARCHAR(32)"
    assert _compiled_type(table.c.error_code).upper() == "VARCHAR(32)"


# --- WORKSPACE_OWNED_MODELS + re-export ---------------------------------------


def test_both_models_registered_workspace_owned() -> None:
    assert ScrapeJob in WORKSPACE_OWNED_MODELS
    assert ScrapeJobTarget in WORKSPACE_OWNED_MODELS


def test_both_models_reexported_from_app_shared_models() -> None:
    from app_shared.models import ScrapeJob as ReexportedJob
    from app_shared.models import ScrapeJobTarget as ReexportedTarget

    assert ReexportedJob is ScrapeJob
    assert ReexportedTarget is ScrapeJobTarget


# --- constraint/index name length (<=63 bytes) --------------------------------


def test_all_constraint_and_index_names_are_within_63_bytes() -> None:
    for table in (ScrapeJob.__table__, ScrapeJobTarget.__table__):
        for constraint in table.constraints:
            if constraint.name is not None:
                assert len(constraint.name.encode("utf-8")) <= 63, constraint.name
        for index in table.indexes:
            if index.name is not None:
                assert len(index.name.encode("utf-8")) <= 63, index.name
