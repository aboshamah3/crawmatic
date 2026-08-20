"""RLS DDL-emitter tests (FR-007, [analyze I2]).

Pure string assertions against the rendered DDL — no database. The key
proof is the fail-closed predicate: ``NULLIF(current_setting(
'app.workspace_id', true), '')::uuid`` must be present verbatim so an
empty (as well as absent) workspace context maps to ``NULL`` (zero
rows) instead of raising ``invalid input syntax for type uuid: ""``.
"""

from __future__ import annotations

from app_shared.models import emit_rls_policy


def test_emit_rls_policy_returns_three_statements() -> None:
    statements = emit_rls_policy("some_table")
    assert len(statements) == 3


def test_emit_rls_policy_enables_and_forces_rls() -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy("some_table")
    assert "ALTER TABLE some_table ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert "ALTER TABLE some_table FORCE ROW LEVEL SECURITY" in force_stmt


def test_emit_rls_policy_predicate_is_fail_closed_via_nullif() -> None:
    _, _, policy_stmt = emit_rls_policy("some_table")

    assert "CREATE POLICY" in policy_stmt
    assert "some_table" in policy_stmt
    # The NULLIF(..., '') wrapper is REQUIRED: it is what makes both an
    # absent AND an empty app.workspace_id map to NULL (zero rows)
    # instead of raising `''::uuid` on an empty context.
    assert "NULLIF(" in policy_stmt
    assert ", '')" in policy_stmt
    assert (
        "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        in policy_stmt
    )


def test_emit_rls_policy_default_policy_name() -> None:
    _, _, policy_stmt = emit_rls_policy("some_table")
    assert "CREATE POLICY some_table_workspace_isolation ON some_table" in policy_stmt


def test_emit_rls_policy_custom_policy_name_and_column() -> None:
    _, _, policy_stmt = emit_rls_policy(
        "other_table", workspace_column="tenant_id", policy_name="custom_policy"
    )
    assert "CREATE POLICY custom_policy ON other_table" in policy_stmt
    assert "tenant_id = NULLIF(" in policy_stmt


# =====================================================================
# Partition RLS inheritance (security review A1, 2026-08-20)
# =====================================================================


def _rls_roles_sql_partition_section() -> str:
    """The `partition-rls-inheritance` block as it stands in the SQL file."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[2] / "scripts" / "sql" / "rls_roles.sql"
    ).read_text(encoding="utf-8")
    begin = sql.index("-- BEGIN partition-rls-inheritance\n") + len(
        "-- BEGIN partition-rls-inheritance\n"
    )
    end = sql.index("\n-- END partition-rls-inheritance")
    return sql[begin:end]


def test_partition_rls_inheritance_sql_matches_the_provisioning_script_byte_for_byte() -> None:
    """The SQL file cannot import Python, so the statement exists twice.

    `scripts/sql/rls_roles.sql` is executed by both provisioning paths
    (`migrate.provision_roles` and `psql -f scripts/rls_provision.sql`);
    the Python constant is what the migration and the runtime
    partition-creation job run. Two copies of a security control is how
    one of them silently stops being the control — so this test is the
    thing that keeps them one statement.
    """
    from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL

    assert _rls_roles_sql_partition_section() == PARTITION_RLS_INHERITANCE_SQL


def test_partition_rls_inheritance_sql_has_no_percent_or_colon_placeholders() -> None:
    """The same text is run three ways, and two of them would choke.

    psycopg3 scans a statement for ``%s``-style placeholders whenever a
    parameter set is supplied — which is why `provision_roles` has to
    reach for a raw cursor to run the ``format(..., %I)`` DO blocks in
    the rest of `rls_roles.sql`. SQLAlchemy's ``text()`` likewise treats
    ``:name`` as a bind parameter. Keeping this block free of both is
    what lets `create_missing_partitions` execute it as an ordinary
    ``session.execute(text(...))`` and the migration as a plain
    ``op.execute(...)``.
    """
    from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL

    assert "%" not in PARTITION_RLS_INHERITANCE_SQL
    assert ":" not in PARTITION_RLS_INHERITANCE_SQL.replace(":=", "")


def test_partition_rls_inheritance_sql_mirrors_the_parent_rather_than_restating_a_policy() -> None:
    """It must read `pg_policy`, not hardcode the workspace predicate.

    A hardcoded policy here would be a second definition of every
    parent's rules — and would give a dual-scope parent's partition the
    strict single policy instead of its read/write pair, silently hiding
    the global rows that parent exists to expose.
    """
    from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL

    assert "pg_policy" in PARTITION_RLS_INHERITANCE_SQL
    assert "pg_get_expr" in PARTITION_RLS_INHERITANCE_SQL
    assert "current_setting" not in PARTITION_RLS_INHERITANCE_SQL
    assert "ENABLE ROW LEVEL SECURITY" in PARTITION_RLS_INHERITANCE_SQL
    assert "FORCE ROW LEVEL SECURITY" in PARTITION_RLS_INHERITANCE_SQL
