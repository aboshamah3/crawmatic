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


#: The one canonical statement that makes a PARTITION carry its parent's
#: row-level security (security review finding A1, 2026-08-20).
#:
#: **Why this is needed at all.** A partitioned parent's policies are
#: applied to a query that names the PARENT. A query that names a CHILD
#: PARTITION is checked against that child's OWN policies — and
#: `CREATE TABLE ... PARTITION OF` gives a child none. `crawmatic_app`
#: holds SELECT on every partition (the blanket `GRANT ... ON ALL TABLES
#: IN SCHEMA public`), so `SELECT * FROM request_attempts_2026_08`
#: returned every workspace's rows to a tenant connection while
#: `SELECT * FROM request_attempts` returned one workspace's. The
#: isolation boundary was defeated by spelling the table name
#: differently. `create_missing_partitions` used to state the opposite in
#: its own docstring — true when planning through the parent, false for a
#: direct hit — which is why every new month reopened the hole.
#:
#: **Why it MIRRORS the parent rather than re-emitting a fixed policy.**
#: A partition of a dual-scope table (:func:`emit_global_readable_rls_policy`)
#: needs that table's read/write PAIR, not the strict single policy — so
#: a hardcoded policy here would be a second, silently-diverging
#: definition of every parent's rules. Reading `pg_policy` instead means
#: the child's posture is derived from the parent's, and cannot drift
#: from it by construction.
#:
#: Idempotent (a policy already present on the child is skipped), and it
#: contains **no** ``%`` and no ``:`` — so the identical text is legal as
#: a psql script body, as ``op.execute(...)`` in a migration, and as a
#: SQLAlchemy ``text()`` executed with a bound parameter set (psycopg3
#: scans for ``%s``-style placeholders whenever parameters are supplied,
#: which is what rules out the ``format(..., %I)`` style used elsewhere
#: in ``scripts/sql/rls_roles.sql``).
#:
#: `scripts/sql/rls_roles.sql` carries this same block verbatim between
#: its ``BEGIN/END partition-rls-inheritance`` markers, so the psql
#: provisioning path applies it too; `tests/unit/test_rls_policy.py`
#: asserts the two are the same statement.
PARTITION_RLS_INHERITANCE_SQL = """DO $$
DECLARE
    part         record;
    pol          record;
    roles_clause text;
    stmt         text;
BEGIN
    FOR part IN
        SELECT child.oid AS child_oid,
               parent.oid AS parent_oid,
               quote_ident(ns.nspname) || '.' || quote_ident(child.relname) AS child_ident
        FROM pg_inherits
        JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
        JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
        WHERE ns.nspname = 'public'
          AND child.relkind = 'r'
          AND child.relispartition
          AND parent.relrowsecurity
    LOOP
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' ENABLE ROW LEVEL SECURITY';
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' FORCE ROW LEVEL SECURITY';

        FOR pol IN
            SELECT p.polname,
                   p.polpermissive,
                   p.polroles,
                   CASE p.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                   END AS cmd_text,
                   pg_get_expr(p.polqual, p.polrelid) AS using_expr,
                   pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
            FROM pg_policy p
            WHERE p.polrelid = part.parent_oid
        LOOP
            CONTINUE WHEN EXISTS (
                SELECT 1 FROM pg_policy q
                WHERE q.polrelid = part.child_oid
                  AND q.polname = pol.polname
            );

            SELECT string_agg(quote_ident(r.rolname), ', ')
              INTO roles_clause
              FROM pg_roles r
             WHERE r.oid = ANY (pol.polroles);

            stmt := 'CREATE POLICY ' || quote_ident(pol.polname)
                 || ' ON ' || part.child_ident
                 || CASE WHEN pol.polpermissive THEN ' AS PERMISSIVE' ELSE ' AS RESTRICTIVE' END
                 || ' FOR ' || pol.cmd_text;

            IF roles_clause IS NOT NULL THEN
                stmt := stmt || ' TO ' || roles_clause;
            END IF;
            IF pol.using_expr IS NOT NULL THEN
                stmt := stmt || ' USING (' || pol.using_expr || ')';
            END IF;
            IF pol.check_expr IS NOT NULL THEN
                stmt := stmt || ' WITH CHECK (' || pol.check_expr || ')';
            END IF;

            EXECUTE stmt;
        END LOOP;
    END LOOP;
END
$$;"""


def emit_partition_rls_inheritance() -> str:
    """Return :data:`PARTITION_RLS_INHERITANCE_SQL` — the single idempotent
    statement that gives every partition in ``public`` the row-level
    security posture of its own parent.

    Callers, all three of which are required (a fix applied in only one
    place is a fix that lapses):

    * the migration that closed the eight partitions that already
      existed;
    * :func:`app_shared.maintenance.partitions.create_missing_partitions`,
      so a partition created at runtime — every month, forever — is born
      isolated rather than repaired later;
    * ``migrate.provision_roles``, the deploy step, which applies it and
      then *verifies* the result from the ``workspace_id`` column.
    """
    return PARTITION_RLS_INHERITANCE_SQL
