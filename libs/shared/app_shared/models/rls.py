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


def emit_global_readable_rls_policy(
    table_name: str,
    *,
    workspace_column: str = "workspace_id",
) -> tuple[str, ...]:
    """Return the DDL statements for a **dual-scope** table's RLS pair (SPEC-06).

    Per ``contracts/rls-global-readable.md`` (research D2/D4, FR-021):
    unlike :func:`emit_rls_policy` (which makes a ``NULL``-workspace row
    invisible to everyone, since ``NULL = ctx`` is never true), this
    emitter is for tables where ``{workspace_column} IS NULL`` marks a
    **global** row that must be readable by every workspace while
    remaining unwritable through the tenant path.

    Returns exactly four statements, in order:

    1. ``ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;``
    2. ``ALTER TABLE {t} FORCE ROW LEVEL SECURITY;``
    3. A ``FOR SELECT`` policy (``{t}_workspace_read``) using
       ``({col} IS NULL OR {col} = <ctx>)`` — own rows **or** any global
       row.
    4. A ``FOR ALL`` write policy (``{t}_workspace_write``) using
       ``{col} = <ctx>`` on **both** ``USING`` and ``WITH CHECK`` — a
       tenant can INSERT/UPDATE/DELETE only its own rows, never a global
       (``NULL``) one.

    The same fail-closed ``NULLIF(current_setting('app.workspace_id',
    true), '')::uuid`` context expression as :func:`emit_rls_policy` is
    reused in both policies: with no workspace context set, ``ctx`` is
    ``NULL`` — own rows fail closed (0 rows / no writes), but global
    rows remain visible via the ``IS NULL`` disjunct in the read policy.
    """
    ctx = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    read_policy = f"{table_name}_workspace_read"
    write_policy = f"{table_name}_workspace_write"
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;",
        (
            f"CREATE POLICY {read_policy} ON {table_name} FOR SELECT "
            f"USING ({workspace_column} IS NULL OR {workspace_column} = {ctx});"
        ),
        (
            f"CREATE POLICY {write_policy} ON {table_name} FOR ALL "
            f"USING ({workspace_column} = {ctx}) "
            f"WITH CHECK ({workspace_column} = {ctx});"
        ),
    )


def emit_fk_transitive_rls_policy(
    table_name: str,
    *,
    parent_table: str,
    fk_column: str,
    parent_pk: str = "id",
    workspace_column: str = "workspace_id",
    policy_name: str | None = None,
) -> tuple[str, ...]:
    """Return the DDL statements enabling fail-closed RLS **transitively via a parent**.

    Per ``contracts/rls-and-migration.md`` (SPEC-12 research D3, FR-026):
    some tables (e.g. ``strategy_attempt_stats``) deliberately carry no
    ``workspace_id`` column of their own — isolation is anchored through
    a real FK to a workspace-owned parent instead. Returns exactly three
    statements, mirroring :func:`emit_rls_policy`'s ENABLE/FORCE/CREATE
    shape:

    1. ``ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;``
    2. ``ALTER TABLE {table} FORCE ROW LEVEL SECURITY;`` — applies even
       to the table owner.
    3. ``CREATE POLICY {policy} ON {table} USING (EXISTS (SELECT 1 FROM
       {parent_table} p WHERE p.{parent_pk} = {table}.{fk_column} AND
       p.{workspace_column} = NULLIF(current_setting('app.workspace_id',
       true), '')::uuid));``

    **Fail-closed** — the same ``NULLIF(current_setting('app.workspace_id',
    true), '')::uuid`` guard as :func:`emit_rls_policy`: with no
    workspace context set, the inner predicate is never true for any
    parent row, so the ``EXISTS`` subquery is never true either — zero
    rows, never an error, never all rows (SC-005).
    """
    policy = policy_name or f"{table_name}_workspace_isolation"
    ctx = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;",
        (
            f"CREATE POLICY {policy} ON {table_name} "
            f"USING (EXISTS (SELECT 1 FROM {parent_table} p "
            f"WHERE p.{parent_pk} = {table_name}.{fk_column} "
            f"AND p.{workspace_column} = {ctx}));"
        ),
    )
