"""Row-level-security DDL emitter for workspace-owned tables.

Per ``contracts/rls.md`` / research.md D5 (§32): this module renders the
three DDL statements needed to enable fail-closed row-level security on
a workspace-owned table. It is a pure string renderer — it does not
execute anything itself; callers ``op.execute(stmt)`` each returned
statement inside the SAME Alembic migration that creates the table.

**Scope in SPEC-02**: delivered and validated only via rendered-DDL
string assertions (``tests/unit/test_rls_policy.py``). No real
workspace-owned table exists yet, so no live isolation surface is
created by this feature — the first concrete application is SPEC-03.
"""

from __future__ import annotations


def emit_rls_policy(
    table_name: str,
    *,
    workspace_column: str = "workspace_id",
    policy_name: str | None = None,
) -> tuple[str, ...]:
    """Return the DDL statements enabling fail-closed RLS on ``table_name``.

    Returns exactly three statements, in order:

    1. ``ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;``
    2. ``ALTER TABLE {table} FORCE ROW LEVEL SECURITY;`` — the policy
       applies even to the table owner.
    3. ``CREATE POLICY {policy} ON {table} USING ({col} =
       NULLIF(current_setting('app.workspace_id', true), '')::uuid);``

    **Fail-closed semantics** ([analyze I2]): ``current_setting(...,
    true)`` returns ``NULL`` when the GUC is unset and ``''`` when set
    to empty. The ``NULLIF(..., '')`` wrapper maps BOTH cases to
    ``NULL``, so the cast never raises ``invalid input syntax for type
    uuid: ""`` and ``{col} = NULL`` is ``NULL`` (never true) — an
    absent or empty workspace context matches **zero rows**, never all
    rows and never an error.

    Application code sets the context per-transaction with
    ``SET LOCAL app.workspace_id = '<uuid>'`` (safe under PgBouncer
    transaction pooling).
    """
    policy = policy_name or f"{table_name}_workspace_isolation"
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;",
        (
            f"CREATE POLICY {policy} ON {table_name} "
            f"USING ({workspace_column} = "
            "NULLIF(current_setting('app.workspace_id', true), '')::uuid);"
        ),
    )
