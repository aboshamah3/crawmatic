"""Control-plane ORM shape tests (EPA C1, 2026-09-03).

Pure ORM/metadata assertions plus an OFFLINE alembic render — no
database is contacted by any test in this module.

Covers the two schema changes the SaaS control plane needs from the
engine:

1. ``control_plane_rules`` — the workspace-owned table that records
   *what the SaaS asked for* (a MONITOR or REPRICE rule on a cadence,
   over a set of the SaaS's own external product ids) and links it to
   the engine's own :class:`~app_shared.models.refresh_rules.RefreshRule`
   that actually drives the scrape. The link is nullable and
   ``ON DELETE SET NULL``: deleting the engine rule must not delete the
   record of what the customer asked for, it must only record that the
   engine rule is gone.
2. ``workspace_entitlements.product_ceiling`` — the nullable per-plan
   product cap the control-plane admission check reads. ``NULL`` means
   "no ceiling recorded", deliberately distinct from ``0`` ("this plan
   allows zero products"), which is why the column is nullable rather
   than ``NOT NULL DEFAULT 0``.

The offline-render tests mirror ``tests/unit/test_migration_offline_abuse_limit.py``:
``alembic upgrade head --sql`` in offline mode, asserting the DDL the
model shape implies is actually emitted (a model that no migration
creates is a table that does not exist in production).
"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app_shared.models import ControlPlaneRule, WorkspaceEntitlement
from app_shared.models.base import TimestampMixin, WorkspaceScopedBase

REPO_ROOT = Path(__file__).resolve().parents[2]

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {
        fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)
    }


def _unique_constraints(table) -> dict[str, UniqueConstraint]:
    return {
        uq.name: uq for uq in table.constraints if isinstance(uq, UniqueConstraint)
    }


# --- ControlPlaneRule --------------------------------------------------------


def test_control_plane_rule_table_name_and_columns() -> None:
    table = ControlPlaneRule.__table__
    assert table.name == "control_plane_rules"
    assert set(table.c.keys()) == {
        "id",
        "workspace_id",
        "external_id",
        "kind",
        "cadence",
        "enabled",
        "target_external_ids",
        "owner_tag",
        "quarantined",
        "quarantine_reason",
        "grace_until",
        "refresh_rule_id",
        "created_at",
        "updated_at",
    }


def test_control_plane_rule_uses_workspace_scoped_base_and_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in ControlPlaneRule.__mro__
    assert TimestampMixin in ControlPlaneRule.__mro__
    assert ControlPlaneRule.__table__.c.workspace_id.nullable is False


def test_control_plane_rule_single_column_pk() -> None:
    assert list(ControlPlaneRule.__table__.primary_key.columns.keys()) == ["id"]


def test_control_plane_rule_external_id_is_unique_per_workspace() -> None:
    """The SaaS's own rule id is the idempotency key for the upsert the
    control-plane route performs — unique WITHIN a workspace, never
    globally (two tenants may both call their rule ``rule-1``)."""
    uniques = _unique_constraints(ControlPlaneRule.__table__)
    assert "uq_control_plane_rules_workspace_id_external_id" in uniques
    constraint = uniques["uq_control_plane_rules_workspace_id_external_id"]
    assert list(constraint.columns.keys()) == ["workspace_id", "external_id"]


def test_control_plane_rule_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ControlPlaneRule.__table__)
    assert "fk_control_plane_rules_workspace_id_workspaces" in fks


def test_control_plane_rule_refresh_rule_fk_targets_refresh_rules_and_sets_null() -> None:
    """Deleting the engine rule must NOT delete the customer's record of
    what they asked for — it must only null the link."""
    fks = _fk_constraints(ControlPlaneRule.__table__)
    assert "fk_control_plane_rules_refresh_rule_id_refresh_rules" in fks
    fk = fks["fk_control_plane_rules_refresh_rule_id_refresh_rules"]
    assert [e.target_fullname for e in fk.elements] == ["refresh_rules.id"]
    assert fk.ondelete == "SET NULL"
    assert ControlPlaneRule.__table__.c.refresh_rule_id.nullable is True


def test_control_plane_rule_nullability() -> None:
    table = ControlPlaneRule.__table__
    for col in ("external_id", "kind", "cadence", "enabled", "target_external_ids", "quarantined"):
        assert table.c[col].nullable is False, col
    for col in ("owner_tag", "quarantine_reason", "grace_until", "refresh_rule_id"):
        assert table.c[col].nullable is True, col


def test_control_plane_rule_target_external_ids_is_jsonb_defaulting_to_empty_list() -> None:
    column = ControlPlaneRule.__table__.c.target_external_ids
    assert _compiled_type(column) == "JSONB"
    assert column.default is not None
    assert column.default.is_callable
    assert column.server_default is not None
    assert "[]" in str(column.server_default.arg)


def test_control_plane_rule_quarantined_defaults_to_false() -> None:
    """A rule is born un-quarantined; quarantine is an explicit act with
    a reason, never an accident of a missing default."""
    column = ControlPlaneRule.__table__.c.quarantined
    assert column.default is not None
    assert column.default.arg is False
    assert column.server_default is not None
    assert "false" in str(column.server_default.arg).lower()


def test_control_plane_rule_enabled_defaults_to_true() -> None:
    column = ControlPlaneRule.__table__.c.enabled
    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg is True


def test_control_plane_rule_grace_until_is_timezone_aware() -> None:
    assert _compiled_type(ControlPlaneRule.__table__.c.grace_until) == (
        "TIMESTAMP WITH TIME ZONE"
    )


def test_control_plane_rule_constraint_and_index_names_fit_postgres_identifier_cap() -> None:
    table = ControlPlaneRule.__table__
    names = [c.name for c in table.constraints if c.name is not None]
    names.extend(ix.name for ix in table.indexes if ix.name is not None)
    for name in names:
        assert len(str(name).encode("utf-8")) <= 63, name


def test_control_plane_rule_is_registered_on_the_shared_metadata() -> None:
    """Without this, Alembic's ``target_metadata`` never sees the table
    and a later autogenerate reads it as a table to DROP."""
    from app_shared.models import Base

    assert "control_plane_rules" in Base.metadata.tables


# --- WorkspaceEntitlement.product_ceiling ------------------------------------


def test_workspace_entitlement_product_ceiling_is_nullable_integer() -> None:
    """NULL means "no ceiling recorded" — deliberately NOT the same as
    ``0`` ("this plan allows zero products"), which is why the column is
    nullable rather than NOT NULL DEFAULT 0."""
    column = WorkspaceEntitlement.__table__.c.product_ceiling
    assert column.nullable is True
    assert _compiled_type(column) == "INTEGER"


def test_workspace_entitlement_table_name_is_unchanged() -> None:
    assert WorkspaceEntitlement.__table__.name == "workspace_entitlements"


# --- Offline alembic render (no database) ------------------------------------


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@lru_cache(maxsize=1)
def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def test_offline_render_creates_control_plane_rules_with_its_unique_key() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE control_plane_rules" in sql
    assert (
        "CONSTRAINT uq_control_plane_rules_workspace_id_external_id "
        "UNIQUE (workspace_id, external_id)" in sql
    )
    assert "FOREIGN KEY(refresh_rule_id) REFERENCES refresh_rules (id) ON DELETE SET NULL" in sql


def test_offline_render_enables_rls_on_control_plane_rules() -> None:
    """A workspace-owned table without a policy is a cross-tenant read —
    the policy ships in the SAME migration that creates the table."""
    sql = _upgrade_sql()
    assert "ALTER TABLE control_plane_rules ENABLE ROW LEVEL SECURITY;" in sql
    assert "ALTER TABLE control_plane_rules FORCE ROW LEVEL SECURITY;" in sql
    assert "CREATE POLICY control_plane_rules_workspace_isolation ON control_plane_rules" in sql


def test_offline_render_adds_product_ceiling_to_workspace_entitlements() -> None:
    sql = _upgrade_sql()
    assert "ALTER TABLE workspace_entitlements ADD COLUMN product_ceiling INTEGER" in sql


def test_offline_render_grants_the_app_role_on_control_plane_rules() -> None:
    """The API process runs as the ordinary app role; without the grant
    the very first control-plane write raises."""
    sql = _upgrade_sql()
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON control_plane_rules" in sql
    assert "TO crawmatic_app" in sql


def test_the_grant_is_role_conditional_so_a_role_less_database_can_migrate() -> None:
    """A bare GRANT aborts the whole upgrade on a database that has not
    been role-provisioned yet, stopping every LATER revision too."""
    sql = _upgrade_sql()
    assert "SELECT FROM pg_roles WHERE rolname = 'crawmatic_app'" in sql


def test_alembic_reports_exactly_one_head_and_it_is_this_revision() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, heads
    assert heads[0].startswith("c8d2e3f4a5b6"), heads
