"""Negative tenant-isolation suite over the REVIEWED manifest (READY-007 / P0.5).

    uv run pytest tests/integration/test_tenant_isolation_roles.py -v

## What this proves, and why the existing RLS test is not enough

`tests/integration/test_rls_cross_workspace.py` proves isolation on
`users` and `products` — two tables, hand-picked. This file proves it on
**every relation the reviewed manifest classifies as tenant-owned**, and
on **every monthly partition of each of them**, by seeding one row per
workspace in each and asserting the ordinary `crawmatic_app` connection
cannot see the other workspace's row. Parametrisation comes from
`scripts/rls_table_manifest.txt` cross-checked against the live catalog,
so a table added by a future migration arrives here automatically: the
manifest check fails until somebody annotates it, and the isolation
check then covers it.

That distinction matters because isolation has never failed uniformly in
this codebase. It failed on the relations nobody enumerated — the eight
monthly partitions that carried `workspace_id` with no RLS at all
(2026-08-20 security review), invisible to a check that asked "of the
tables where RLS is ON, which lack FORCE?".

## The four properties

A. **No manifest relation leaks.** Seeded row for workspace A, context
   set to workspace B, query by primary key: 0 rows. Parametrised over
   every WORKSPACE/TRANSITIVE manifest relation AND every partition
   child of one. A positive control runs first — under workspace A's own
   context the same query returns exactly 1 — because a broken
   connection also returns zero rows, and a suite that cannot tell those
   two apart proves nothing.

B. **Fail closed.** With no `app.workspace_id` set at all, the same
   query returns 0 — not "everything", which is what a policy written
   without the `NULLIF(..., '')` guard would do on an unset GUC.

C. **The role cannot lift its own confinement.** `crawmatic_app` has
   `rolbypassrls = false`, is not a superuser, owns no relation in
   `public`, and is refused both `ALTER TABLE ... NO FORCE ROW LEVEL
   SECURITY` and `TRUNCATE` (which row policies do not filter at all).

A2. **The `refresh_tokens` seam.** A policy on a credentials table is
   only half a fix: the other half is that the pre-auth statements which
   must still work — rotate, revoke — keep working, on the BYPASSRLS
   auth role, with no context at all. Both halves are asserted (EPA B8b,
   head `b6d94c2f1a70`), because enabling the policy without moving the
   router onto `get_auth_session()` fails silently rather than loudly:
   every refresh would simply match zero rows and read as a bad token.

D. **Pool checkout does not carry context between tenants.** A
   session-scoped `SET app.workspace_id` on one pooled connection must
   not still be set when the next checkout gets that same connection.
   The application always uses `SET LOCAL` semantics
   (`app_shared.database.set_workspace_context` passes `is_local=true`),
   so this is the second line: if anything ever emits a plain `SET`, the
   pool must scrub it, because the next checkout is a different tenant's
   request. Note that a plain `SET` survives `ROLLBACK`, which is
   SQLAlchemy's default `reset_on_return` — so this property does not
   hold for free.

## Provisioning

The suite provisions everything itself, from zero, exactly the way the
deploy step does: `alembic upgrade head`, then
`scripts/provision_db_roles.py`'s `provision()` (which executes
`scripts/provision_db_roles.sql`), then `verify()` — and it asserts the
posture has no FAIL findings BEFORE making any isolation claim, since a
misprovisioned role would make every assertion below vacuous.

It never touches `.env` DSNs: the only URL it accepts is an explicit
throwaway one.

    docker run -d --name cm-b8-pg \\
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55488:5432 postgres:18-alpine

    TENANT_ISOLATION_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55488/crawmatic \\
      uv run pytest tests/integration/test_tenant_isolation_roles.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from provision_db_roles import (  # noqa: E402
    APP_ROLE,
    MIGRATE_ROLE,
    SYSTEM_ROLE,
    TENANT_CLASSES,
    load_manifest,
    provision,
    read_catalog,
    read_only_engine,
    verify,
)

#: Throwaway, obviously-local credentials. This suite only ever runs
#: against a scratch database (a CI service container or a local
#: `docker run`), and it writes these passwords into that database
#: itself as part of provisioning.
_APP_PW = "b8-isolation-app-pw"  # noqa: S105 - throwaway test database only
_AUTH_PW = "b8-isolation-auth-pw"  # noqa: S105 - throwaway test database only
_MIGRATE_PW = "b8-isolation-migrate-pw"  # noqa: S105 - throwaway test database only

_HOWTO = """
No reachable owner/admin database URL for the tenant-isolation suite.

Set TENANT_ISOLATION_TEST_DATABASE_URL (or RLS_TEST_DATABASE_URL /
MIGRATION_DATABASE_URL) to an owner-role URL for a THROWAWAY Postgres:

  docker run -d --name cm-b8-pg \\
    -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
    -e POSTGRES_DB=crawmatic -p 127.0.0.1:55488:5432 postgres:18-alpine

  TENANT_ISOLATION_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55488/crawmatic \\
    uv run pytest tests/integration/test_tenant_isolation_roles.py -v

The suite migrates the database and provisions the three roles itself.
"""


# =====================================================================
# Provisioning fixtures
# =====================================================================


def _admin_url() -> str | None:
    return (
        os.environ.get("TENANT_ISOLATION_TEST_DATABASE_URL")
        or os.environ.get("RLS_TEST_DATABASE_URL")
        or os.environ.get("MIGRATION_DATABASE_URL")
    )


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        engine = create_engine(url)
        with engine.connect():
            pass
        engine.dispose()
    except Exception:
        return False
    return True


def _with_role(url: str, username: str, password: str) -> str:
    return (
        make_url(url)
        .set(username=username, password=password)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def provisioned() -> Iterator[dict[str, str]]:
    """A migrated database with the three-role posture applied from zero."""
    admin_url = _admin_url()
    if not _reachable(admin_url):
        pytest.skip(_HOWTO)
    assert admin_url is not None

    engine = create_engine(admin_url)
    try:
        with engine.connect() as conn:
            has_schema = bool(
                conn.execute(text("SELECT to_regclass('public.users') IS NOT NULL")).scalar()
            )
    finally:
        engine.dispose()

    if not has_schema:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "MIGRATION_DATABASE_URL": admin_url},
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert result.returncode == 0, (
            f"alembic upgrade head failed\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    provision(
        admin_url,
        app_password=_APP_PW,
        auth_password=_AUTH_PW,
        migrate_password=_MIGRATE_PW,
    )

    # The posture is an ASSERTION, not an assumption. If crawmatic_app
    # came out BYPASSRLS, or owning a relation, every isolation claim in
    # this file would be vacuously true.
    failures = [f for f in verify(admin_url) if f.severity == "FAIL"]
    assert failures == [], "role posture is wrong before any isolation is claimed:\n" + "\n".join(
        f.render() for f in failures
    )

    yield {
        "admin_url": admin_url,
        "app_url": _with_role(admin_url, APP_ROLE, _APP_PW),
        "system_url": _with_role(admin_url, SYSTEM_ROLE, _AUTH_PW),
    }


# =====================================================================
# Catalog-driven two-tenant seeding
# =====================================================================

#: Columns whose synthesized value must satisfy a CHECK constraint that
#: generic type-based synthesis cannot know about. Keyed by table.
#:
#: `refresh_rules` is the only table in the schema with CHECK
#: constraints (three of them): exactly one of cron_expression /
#: interval_minutes must be non-null, and `scope` must agree with which
#: target FK is set. A WORKSPACE-scoped rule with a cron expression and
#: every target FK left NULL satisfies all three.
_COLUMN_OVERRIDES: dict[str, dict[str, object]] = {
    "refresh_rules": {
        "scope": "WORKSPACE",
        "cron_expression": "0 * * * *",
        "interval_minutes": None,
        "product_id": None,
        "product_variant_id": None,
        "product_group_id": None,
        "competitor_id": None,
        "match_id": None,
    },
}

#: Tables with no `workspace_id` column reach their tenant through a
#: parent row. The seeder must populate that FK even when it is
#: nullable, or the row would be scoped to nothing and the isolation
#: assertion would be meaningless.
_TRANSITIVE_PARENT_FK = {
    "match_audit_classifications": "match_id",
    "match_competitor_identifiers": "match_id",
    "strategy_attempt_stats": "domain_strategy_profile_id",
}

_COLUMNS_SQL = """
SELECT a.attname                                         AS name,
       format_type(a.atttypid, a.atttypmod)              AS type,
       a.attnotnull                                      AS notnull,
       (a.atthasdef OR a.attidentity <> '')              AS has_default,
       a.attgenerated <> ''                              AS generated
FROM pg_attribute a
WHERE a.attrelid = to_regclass('public.' || :table)
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum
"""

_PROBE_COLUMN_SQL = """
SELECT c.relname AS table_name,
       coalesce(
           (SELECT a.attname FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attname = 'id' AND NOT a.attisdropped),
           (SELECT a.attname FROM pg_constraint pk
              JOIN LATERAL unnest(pk.conkey) AS k(num) ON true
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.num
             WHERE pk.conrelid = c.oid AND pk.contype = 'p'
             ORDER BY a.attnum LIMIT 1)
       ) AS probe_column
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p')
"""

_FKS_SQL = """
SELECT rel.relname                       AS table_name,
       att.attname                       AS column_name,
       ref.relname                       AS ref_table,
       refatt.attname                    AS ref_column
FROM pg_constraint c
JOIN pg_class rel        ON rel.oid = c.conrelid
JOIN pg_class ref        ON ref.oid = c.confrelid
JOIN LATERAL unnest(c.conkey, c.confkey) AS k(src, dst) ON true
JOIN pg_attribute att    ON att.attrelid = c.conrelid AND att.attnum = k.src
JOIN pg_attribute refatt ON refatt.attrelid = c.confrelid AND refatt.attnum = k.dst
WHERE c.contype = 'f'
  AND rel.relnamespace = 'public'::regnamespace
"""


def _synthesize(pg_type: str, tag: str) -> object:
    """A value that satisfies the column's type. Uniqueness comes from ``tag``."""
    base = pg_type.split("(")[0].strip().lower()
    if base.endswith("[]"):
        return "{}"
    if base == "uuid":
        return str(uuid.uuid4())
    if base in ("text", "character varying", "varchar", "character", "char", "citext", "name"):
        value = f"b8-{tag}-{uuid.uuid4().hex[:12]}"
        # Respect a declared length. Several columns are `character(3)`
        # (currency codes) or `varchar(32)`, and the tail of a uuid hex
        # keeps the truncated value unique enough for the UNIQUE
        # constraints these columns take part in.
        if "(" in pg_type:
            limit = int(pg_type.split("(")[1].split(")")[0].split(",")[0])
            value = value[-limit:] if limit < len(value) else value
        return value
    if base in ("integer", "bigint", "smallint"):
        return 1
    if base in ("numeric", "decimal", "real", "double precision"):
        return 1.0
    if base == "boolean":
        return False
    if base.startswith("timestamp") or base.startswith("time"):
        return "now()"  # rendered as raw SQL, see _render
    if base == "date":
        return "current_date"  # raw SQL
    if base in ("json", "jsonb"):
        return "{}"
    if base == "interval":
        return "1 hour"
    if base == "inet":
        return "127.0.0.1"
    if base == "bytea":
        return b"b8"
    raise NotImplementedError(f"no synthesizer for column type {pg_type!r}")


_RAW_SQL_VALUES = ("now()", "current_date")


class Seeder:
    """Inserts one row per workspace into every tenant relation.

    Catalog-driven on purpose. A hand-written builder per table would
    have to be extended by whoever adds the next table — and the whole
    lesson of the partition incident is that the isolation surface grows
    in places nobody remembers to extend. Here, a new table is seeded
    and asserted the moment it appears in the catalog and the manifest.
    """

    def __init__(self, conn, tables: list[str]) -> None:
        self.conn = conn
        self.tables = tables
        self.fks = self._load_fks()
        self.columns = {t: conn.execute(text(_COLUMNS_SQL), {"table": t}).all() for t in tables}
        #: The column each relation is probed by. `id` where there is
        #: one; otherwise the first primary-key column, because not
        #: every table uses `id` (`dispatch_intents` keys on
        #: `intent_id`) and a suite that assumed otherwise would simply
        #: not cover those tables.
        self.probe_columns: dict[str, str] = {
            row.table_name: row.probe_column
            for row in conn.execute(text(_PROBE_COLUMN_SQL))
            if row.probe_column
        }
        #: (table, workspace_id) -> every column value inserted for that row
        self.rows: dict[tuple[str, str], dict[str, object]] = {}

    def _load_fks(self) -> dict[str, dict[str, set[tuple[str, str]]]]:
        """``{table: {column: {(ref_table, ref_column), ...}}}`` for every FK in public.

        A column maps to a SET of referenced tables because several
        tables carry composite foreign keys of the form
        ``(workspace_id, product_id) -> products (workspace_id, id)`` —
        the pattern that makes a child row provably belong to the same
        workspace as its parent. Splitting those into per-column pairs
        makes `workspace_id` appear to reference five different tables
        at once, so it has to be a set rather than a last-one-wins
        single value.
        """
        out: dict[str, dict[str, set[tuple[str, str]]]] = {}
        for row in self.conn.execute(text(_FKS_SQL)):
            out.setdefault(row.table_name, {}).setdefault(row.column_name, set()).add(
                (row.ref_table, row.ref_column)
            )
        return out

    def _required(self, table: str, column) -> bool:
        """Must this column be supplied for the INSERT to succeed and be scoped?"""
        if column.notnull and not column.has_default:
            return True
        # A TRANSITIVE table's parent FK is nullable, but a row with it
        # NULL is scoped to nothing and would make the isolation
        # assertion meaningless.
        return column.name == _TRANSITIVE_PARENT_FK.get(table)

    def _deps(self, table: str) -> set[str]:
        """Tables that must be seeded before ``table``.

        Only REQUIRED foreign keys create an ordering edge. The nullable
        ones are what make the schema cyclic — `domain_strategy_profiles.
        preferred_method_id` points at `domain_strategy_methods`, whose
        `domain_strategy_profile_id` points back — and a nullable
        pointer is not a seeding dependency, because the row is valid
        with it left NULL.

        `workspace_id` is excluded: its composite FKs name every parent
        table, but the seeder supplies it directly from the workspace
        being seeded, so it never needs a row resolved for it.
        """
        table_fks = self.fks.get(table, {})
        deps: set[str] = set()
        for column in self.columns[table]:
            if column.name == "workspace_id" or not self._required(table, column):
                continue
            deps |= {ref_table for ref_table, _ in table_fks.get(column.name, set())}
        return deps - {table}

    def _order(self) -> list[str]:
        """Topological order over REQUIRED foreign-key dependencies."""
        remaining = list(self.tables)
        done: list[str] = []
        while remaining:
            progressed = False
            for table in list(remaining):
                if self._deps(table) & set(remaining):
                    continue
                done.append(table)
                remaining.remove(table)
                progressed = True
            if not progressed:
                raise AssertionError(
                    "required-foreign-key cycle among tenant tables prevents seeding: "
                    f"{ {t: sorted(self._deps(t) & set(remaining)) for t in sorted(remaining)} }"
                )
        return done

    def _resolve_fk(
        self, refs: set[tuple[str, str]], workspace_id: str
    ) -> object | None:
        """The referenced value from a row of this workspace, or ``None``.

        Same-workspace resolution is not a nicety: the composite
        ``(workspace_id, x_id)`` foreign keys reject a parent row from a
        different workspace, and borrowing one would also silently
        weaken the isolation assertion (a row whose parent belongs to
        the OTHER tenant is not the row this suite means to probe).

        The REFERENCED COLUMN is honoured rather than assumed to be
        ``id`` — ``scrape_job_targets.dispatch_intent_id`` points at
        ``dispatch_intents.intent_id``.
        """
        for ref_table, ref_column in sorted(refs):
            if ref_table == "workspaces":
                return workspace_id
            seeded = self.rows.get((ref_table, workspace_id))
            if seeded is not None and ref_column in seeded:
                return seeded[ref_column]
        return None

    def seed(self, table: str, workspace_id: str) -> object | None:
        columns = self.columns[table]
        names = {c.name for c in columns}
        probe_column = self.probe_columns.get(table)
        assert probe_column, f"{table} has no id/primary key column; the seeder needs one to probe by"

        overrides = _COLUMN_OVERRIDES.get(table, {})
        table_fks = self.fks.get(table, {})
        probe_value = str(uuid.uuid4())

        values: dict[str, object] = {probe_column: probe_value}
        if "workspace_id" in names and "workspace_id" not in values:
            values["workspace_id"] = workspace_id

        for col in columns:
            if col.generated or col.name in values or col.name in overrides:
                continue
            if not self._required(table, col):
                continue
            if col.name in table_fks:
                resolved = self._resolve_fk(table_fks[col.name], workspace_id)
                if resolved is None:
                    return None  # dependency could not be satisfied; skip this table
                values[col.name] = resolved
            else:
                values[col.name] = _synthesize(col.type, table[:10])

        for name, value in overrides.items():
            if name in names:
                values[name] = value

        placeholders: list[str] = []
        params: dict[str, object] = {}
        for name, value in values.items():
            if isinstance(value, str) and value in _RAW_SQL_VALUES:
                placeholders.append(value)
            else:
                placeholders.append(f":{name}")
                params[name] = value
        columns_sql = ", ".join(values)
        sql = (
            f"INSERT INTO {table} ({columns_sql}) "  # noqa: S608 - catalog-sourced names
            f"VALUES ({', '.join(placeholders)})"
        )
        self.conn.execute(text(sql), params)
        # Record the resolved values (not the raw SQL literals) so a
        # child row can honour a foreign key that points at any column,
        # not only `id`.
        self.rows[(table, workspace_id)] = {
            name: value for name, value in values.items() if value not in _RAW_SQL_VALUES
        }
        return probe_value


@pytest.fixture(scope="session")
def manifest() -> dict:
    return load_manifest()


@pytest.fixture(scope="session")
def catalog(provisioned: dict[str, str]) -> dict:
    engine = read_only_engine(provisioned["admin_url"])
    try:
        with engine.connect() as conn:
            return read_catalog(conn)
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def seeded(provisioned: dict[str, str], manifest: dict, catalog: dict) -> Iterator[dict]:
    """Two workspaces with one row per tenant relation each.

    Seeded through the OWNER connection (which the migration role
    represents), never through the app role: the point is to create rows
    the app role must not be able to see, so creating them with the app
    role would beg the question.
    """
    tenant_tables = [
        name
        for name, entry in manifest.items()
        if entry.table_class in TENANT_CLASSES and name in catalog
    ]
    ws_a, ws_b = str(uuid.uuid4()), str(uuid.uuid4())

    engine = create_engine(provisioned["admin_url"])
    seeded_rows: dict[tuple[str, str], tuple[str, object]] = {}
    skipped: list[str] = []
    try:
        with engine.begin() as conn:
            for workspace_id, slug in ((ws_a, "b8-a"), (ws_b, "b8-b")):
                conn.execute(
                    text(
                        "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                        "VALUES (:id, :name, :slug, 'ACTIVE', now(), now())"
                    ),
                    {"id": workspace_id, "name": f"B8 {slug}", "slug": f"{slug}-{uuid.uuid4().hex[:8]}"},
                )

            seeder = Seeder(conn, tenant_tables)
            order = seeder._order()
            for workspace_id in (ws_a, ws_b):
                for table in order:
                    try:
                        probe_value = seeder.seed(table, workspace_id)
                    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                        raise AssertionError(
                            f"could not seed {table!r} for the isolation suite: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    if probe_value is None:
                        skipped.append(table)
                    else:
                        seeded_rows[(table, workspace_id)] = (
                            seeder.probe_columns[table],
                            probe_value,
                        )
            probe_columns = dict(seeder.probe_columns)
    finally:
        engine.dispose()

    yield {
        "workspace_a": ws_a,
        "workspace_b": ws_b,
        #: (table, workspace_id) -> (probe_column, probe_value)
        "rows": seeded_rows,
        "probe_columns": probe_columns,
        "tables": order,
        "skipped": sorted(set(skipped)),
    }


def _probe_relations(manifest: dict, catalog: dict) -> list[tuple[str, str]]:
    """(relation_to_query, table_the_row_was_inserted_into) for every tenant relation.

    Partition CHILDREN are included as their own probe targets against
    the row inserted through the parent. Querying the child by name is
    the exact attack the 2026-08-20 review found: a partition is checked
    against its OWN policies, and `CREATE TABLE ... PARTITION OF` gives
    a child none.
    """
    probes: list[tuple[str, str]] = []
    for name, entry in sorted(manifest.items()):
        if entry.table_class not in TENANT_CLASSES or name not in catalog:
            continue
        probes.append((name, name))
        for child in sorted(c.name for c in catalog.values() if c.parent == name):
            probes.append((child, name))
    return probes


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise the isolation tests over the reviewed manifest.

    Done here rather than with a fixture-derived `parametrize` because
    the parameter set comes from a FILE (the manifest) that is readable
    without a database — so collection works, and reports the real table
    names, even when no Postgres is reachable and the tests skip.
    """
    if "relation" not in metafunc.fixturenames:
        return
    entries = load_manifest()
    names = sorted(n for n, e in entries.items() if e.table_class in TENANT_CLASSES)
    metafunc.parametrize("relation", names)


# =====================================================================
# A. No manifest relation leaks across workspaces
# =====================================================================


def _count_probe(
    engine: Engine,
    relation: str,
    probe: tuple[str, object],
    workspace_id: str | None,
) -> int:
    """Rows visible for the probed key under ``workspace_id``, in one transaction.

    The context is set with `set_config(..., true)` — the `SET LOCAL`
    form `app_shared.database.set_workspace_context` uses and the only
    form that is safe under PgBouncer transaction pooling.

    The query names the relation and filters on its key ONLY. There is
    deliberately no `WHERE workspace_id = ...`: an application-level
    scope clause would prove that the application filters, which is not
    the question. Row-level security is the only thing standing between
    this statement and the other tenant's row.
    """
    column, value = probe
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": "" if workspace_id is None else workspace_id},
        )
        return int(
            conn.execute(
                text(f"SELECT count(*) FROM {relation} WHERE {column} = :v"),  # noqa: S608
                {"v": value},
            ).scalar_one()
        )


@pytest.fixture(scope="session")
def app_engine(provisioned: dict[str, str]) -> Iterator[Engine]:
    engine = create_engine(provisioned["app_url"], connect_args={"prepare_threshold": None})
    yield engine
    engine.dispose()


def test_every_tenant_relation_was_actually_seeded(seeded: dict, manifest: dict) -> None:
    """A relation the suite silently failed to seed would pass every check below."""
    assert seeded["skipped"] == [], (
        "the seeder could not satisfy a dependency for: "
        f"{seeded['skipped']} — those relations are NOT covered by the isolation "
        "assertions and must not be reported as passing"
    )
    for table in seeded["tables"]:
        for workspace in ("workspace_a", "workspace_b"):
            assert (table, seeded[workspace]) in seeded["rows"], (
                f"no row seeded in {table} for {workspace}"
            )


def test_own_context_sees_own_row(
    relation: str, seeded: dict, catalog: dict, app_engine: Engine
) -> None:
    """Positive control: a confined connection must still see its OWN row.

    Without this, the foreign-context assertion below is satisfied by any
    connection that is simply broken.
    """
    if relation not in catalog:
        pytest.skip(f"{relation} not present in this database")
    probe = seeded["rows"][(relation, seeded["workspace_a"])]
    assert _count_probe(app_engine, relation, probe, seeded["workspace_a"]) == 1


def test_foreign_workspace_context_sees_zero_rows(
    relation: str, seeded: dict, catalog: dict, app_engine: Engine
) -> None:
    """A. The app role holding workspace B's context reads 0 of workspace A's rows.

    Also runs against every PARTITION of the relation by name — the
    "spell the table differently" bypass.
    """
    if relation not in catalog:
        pytest.skip(f"{relation} not present in this database")
    probe = seeded["rows"][(relation, seeded["workspace_a"])]
    targets = [relation] + sorted(c.name for c in catalog.values() if c.parent == relation)
    leaked = {
        target: _count_probe(app_engine, target, probe, seeded["workspace_b"])
        for target in targets
    }
    assert not any(leaked.values()), (
        f"RLS DID NOT CONFINE crawmatic_app: workspace B's context read workspace A's "
        f"row through {[t for t, n in leaked.items() if n]}"
    )


def test_no_workspace_context_sees_zero_rows(
    relation: str, seeded: dict, catalog: dict, app_engine: Engine
) -> None:
    """B. Fail closed: an unset `app.workspace_id` matches nothing, not everything."""
    if relation not in catalog:
        pytest.skip(f"{relation} not present in this database")
    probe = seeded["rows"][(relation, seeded["workspace_a"])]
    targets = [relation] + sorted(c.name for c in catalog.values() if c.parent == relation)
    for target in targets:
        assert _count_probe(app_engine, target, probe, None) == 0, (
            f"{target} returned rows with app.workspace_id unset — the policy is not "
            "fail-closed"
        )


# =====================================================================
# A2. refresh_tokens: the policy confines the tenant WITHOUT closing the
#     pre-auth seam the whole login flow depends on
# =====================================================================
#
# `refresh_tokens` was the one GAP the B8 audit recorded, closed by EPA
# B8b at head b6d94c2f1a70 with a TRANSITIVE policy through
# `users.user_id`. Properties A and B above already cover it by
# parametrisation, and this section adds the half a manifest-driven
# suite structurally cannot express: that the statements the API really
# issues still work on the seam they were moved to.
#
# The failure mode being guarded is silent. Enabling this policy without
# moving `apps/api/app/routers/auth.py` onto `get_auth_session()` breaks
# nothing loudly — the rotation simply matches zero rows, and every
# refresh in production answers the uniform "wrong credentials" error
# while every logout answers 204 having revoked nothing.

_LIVE_TOKEN_SQL = """
INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, revoked_at, created_at)
VALUES (:id, :user_id, :token_hash, now() + interval '14 days', NULL, now())
"""


@pytest.fixture()
def live_refresh_token(provisioned: dict[str, str], seeded: dict) -> Iterator[dict]:
    """One LIVE (unexpired, unrevoked) token for workspace A's seeded user.

    The catalog seeder synthesizes `expires_at` as `now()`, which is
    already expired by the time the rotation predicate reads it — fine
    for a visibility probe, useless for exercising the real rotation SQL.
    """
    user_probe = seeded["rows"].get(("users", seeded["workspace_a"]))
    assert user_probe is not None, "the seeder did not create a users row for workspace A"
    _, user_id = user_probe

    token_hash = f"b8b-{uuid.uuid4().hex}"
    token_id = str(uuid.uuid4())
    engine = create_engine(provisioned["admin_url"])
    try:
        with engine.begin() as conn:
            conn.execute(
                text(_LIVE_TOKEN_SQL),
                {"id": token_id, "user_id": user_id, "token_hash": token_hash},
            )
        yield {"id": token_id, "user_id": user_id, "token_hash": token_hash}
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM refresh_tokens WHERE id = :id"), {"id": token_id}
            )
    finally:
        engine.dispose()


def _rotate(engine: Engine, token_hash: str) -> int:
    """Run the REAL rotation statement the API issues; return rows affected."""
    from app_shared.security.tokens import ROTATE_REFRESH_TOKEN_SQL

    with engine.begin() as conn:
        rows = conn.execute(text(ROTATE_REFRESH_TOKEN_SQL), {"token_hash": token_hash}).all()
    return len(rows)


def test_app_role_cannot_read_another_workspaces_refresh_token_hashes(
    live_refresh_token: dict, seeded: dict, app_engine: Engine
) -> None:
    """The gap itself: workspace B's connection reading workspace A's token hashes."""
    probe = ("token_hash", live_refresh_token["token_hash"])
    assert _count_probe(app_engine, "refresh_tokens", probe, seeded["workspace_b"]) == 0
    assert _count_probe(app_engine, "refresh_tokens", probe, None) == 0
    # Positive control: the owning workspace's context does see it, so
    # the two assertions above are confinement and not a broken query.
    assert _count_probe(app_engine, "refresh_tokens", probe, seeded["workspace_a"]) == 1


def test_pre_auth_rotation_still_resolves_on_the_auth_seam(
    provisioned: dict[str, str], live_refresh_token: dict
) -> None:
    """`crawmatic_auth`, with NO workspace context, still rotates the token.

    This is the whole reason the auth role holds BYPASSRLS. The rotation
    is what *resolves* the principal, so there is no `app.workspace_id`
    to set at the moment it runs.
    """
    engine = create_engine(provisioned["system_url"], connect_args={"prepare_threshold": None})
    try:
        assert _rotate(engine, live_refresh_token["token_hash"]) == 1
        # And it is genuinely atomic: the second attempt finds it revoked.
        assert _rotate(engine, live_refresh_token["token_hash"]) == 0
    finally:
        engine.dispose()


def test_pre_auth_rotation_on_the_app_role_matches_nothing(
    live_refresh_token: dict, app_engine: Engine
) -> None:
    """The regression this packet exists to prevent, asserted directly.

    With no workspace context — which is the only state the pre-auth
    rotation ever runs in — the confined role updates zero rows. A router
    that issued this statement on `get_session()` would therefore reject
    every valid refresh token in production, with no error anywhere.
    """
    assert _rotate(app_engine, live_refresh_token["token_hash"]) == 0


# =====================================================================
# C. The role cannot lift its own confinement
# =====================================================================


def test_app_role_is_not_superuser_and_has_no_bypassrls(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    assert row.rolsuper is False, f"{APP_ROLE} is a SUPERUSER — a superuser ignores RLS entirely"
    assert row.rolbypassrls is False, f"{APP_ROLE} has rolbypassrls = true"


def test_app_role_owns_no_relation(app_engine: Engine) -> None:
    """An owner may DROP POLICY and ALTER TABLE ... NO FORCE ROW LEVEL SECURITY."""
    with app_engine.connect() as conn:
        owned = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tableowner = current_user"
                )
            )
            .scalars()
            .all()
        )
    assert owned == [], f"{APP_ROLE} owns {len(owned)} relation(s) in public: {sorted(owned)}"


def test_app_role_cannot_disable_force_rls(app_engine: Engine) -> None:
    with pytest.raises((ProgrammingError, DBAPIError)):
        with app_engine.begin() as conn:
            conn.execute(text("ALTER TABLE products NO FORCE ROW LEVEL SECURITY"))


def test_app_role_cannot_truncate(app_engine: Engine) -> None:
    """TRUNCATE is not filtered by row policies at all, which is why it is not granted."""
    with pytest.raises((ProgrammingError, DBAPIError)):
        with app_engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE products"))


def test_system_role_holds_bypassrls_and_owns_nothing(provisioned: dict[str, str]) -> None:
    """The one sanctioned BYPASSRLS role is still not an owner."""
    engine = create_engine(provisioned["system_url"], connect_args={"prepare_threshold": None})
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
            owned = (
                conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tableowner = current_user"
                    )
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()
    assert row.rolbypassrls is True, f"{SYSTEM_ROLE} lost BYPASSRLS — the pre-auth lookup would 0-row"
    assert row.rolsuper is False, f"{SYSTEM_ROLE} is a SUPERUSER"
    assert owned == [], (
        f"{SYSTEM_ROLE} owns {len(owned)} relation(s) — a BYPASSRLS role that also owns "
        f"tables can drop the policies it already bypasses: {sorted(owned)}"
    )


# =====================================================================
# Catalog vs reviewed manifest
# =====================================================================


def test_catalog_matches_reviewed_manifest(provisioned: dict[str, str]) -> None:
    """No unreviewed relation, and no declaration the catalog contradicts."""
    failures = [f for f in verify(provisioned["admin_url"]) if f.severity == "FAIL"]
    assert failures == [], "\n".join(f.render() for f in failures)


def test_migrate_role_exists_and_cannot_read_tenant_rows(provisioned: dict[str, str]) -> None:
    """The DDL owner is NOBYPASSRLS: owning the schema is not reading the data."""
    engine = create_engine(provisioned["admin_url"])
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                    "WHERE rolname = :role"
                ),
                {"role": MIGRATE_ROLE},
            ).one_or_none()
    finally:
        engine.dispose()
    assert row is not None, f"{MIGRATE_ROLE} was not created by provisioning"
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    assert row.rolcanlogin is True


# =====================================================================
# D. Pool checkout does not carry workspace context between tenants
# =====================================================================


def _current_context(conn) -> str | None:
    value = conn.exec_driver_sql("SELECT current_setting('app.workspace_id', true)").scalar()
    return value or None


def test_session_scoped_context_does_not_survive_pool_checkin(
    provisioned: dict[str, str], seeded: dict
) -> None:
    """D. A leaked `SET` must not reach the next tenant's checkout.

    The precise shape of the leak matters, and it is narrower than it
    first looks. `SET` is transactional in PostgreSQL, so a plain `SET`
    inside a transaction that SQLAlchemy later rolls back at check-in
    (the default `reset_on_return="rollback"`) IS undone — that case is
    already safe. The leak is a plain `SET` in a transaction that
    **commits**: the GUC then belongs to the SESSION, no rollback will
    ever undo it, and the connection returns to the pool still carrying
    one tenant's context. Measured on PostgreSQL 18.6 without the reset
    listener, the next checkout of that same pooled connection reads the
    previous tenant's workspace id back out; with it, it reads nothing.

    So this test commits, deliberately. The application never emits a
    plain `SET` today (`set_workspace_context` passes `is_local=true`),
    and that is the first line of defence. This is the second: it must
    not be possible for one to matter.
    """
    engine = create_engine(
        provisioned["app_url"],
        connect_args={"prepare_threshold": None},
        pool_size=1,
        max_overflow=0,
    )

    # The engine under test is configured the way app_shared.database
    # configures the runtime engine — see `_install_context_reset`.
    from app_shared.database import install_workspace_context_reset

    install_workspace_context_reset(engine)

    try:
        # COMMITTED, not rolled back — see the docstring. A rolled-back
        # SET is undone by Postgres itself and would make this test pass
        # whether or not the reset listener exists.
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SET app.workspace_id = '{seeded['workspace_a']}'")
            assert _current_context(conn) == seeded["workspace_a"]
        # Same single pooled connection comes back out.
        with engine.connect() as conn:
            leaked = _current_context(conn)
        assert leaked is None, (
            f"app.workspace_id={leaked!r} survived pool check-in — the next tenant's "
            "request would start inside the previous tenant's context"
        )
    finally:
        engine.dispose()


def test_runtime_engine_installs_the_context_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset is wired into the ENGINE THE SERVICES USE, not just this test.

    A pool hygiene fix that only exists inside the test that proves it is
    not a fix.
    """
    from types import SimpleNamespace

    import app_shared.database as database_module
    import app_shared.db.rls_guard as rls_guard_module

    # Deliberately does NOT build a real `Settings`: this suite must
    # never depend on a `.env`, and the property under test is which
    # listeners `get_engine` installs, not how it resolves its DSN.
    # `create_engine` is lazy, so no connection is attempted.
    monkeypatch.setattr(
        database_module,
        "get_settings",
        lambda: SimpleNamespace(
            DATABASE_URL="postgresql+psycopg://u:p@127.0.0.1:1/none",
            DB_POOL_SIZE=1,
            DB_MAX_OVERFLOW=0,
        ),
    )
    monkeypatch.setattr(rls_guard_module, "enforce_rls_role_on_startup", lambda engine: None)
    monkeypatch.setattr(database_module, "_engine", None)
    try:
        engine = database_module.get_engine()
        assert event.contains(engine, "checkout", database_module._reset_workspace_context), (
            "app_shared.database.get_engine() does not install the workspace-context "
            "reset on the pool it hands the services"
        )
    finally:
        if database_module._engine is not None:
            database_module._engine.dispose()
        database_module._engine = None
