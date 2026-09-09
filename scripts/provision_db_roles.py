#!/usr/bin/env python
"""Provision and verify the three production database roles (READY-007 / P0.5).

Companion to ``scripts/provision_db_roles.sql`` (the DDL) and
``scripts/rls_table_manifest.txt`` (the reviewed table inventory).

    # provision from scratch into a throwaway/CI Postgres (WRITES)
    PROVISION_DB_ROLES_URL=postgresql+psycopg://owner:pw@127.0.0.1:5432/crawmatic \\
        uv run python scripts/provision_db_roles.py --provision

    # verify an existing database — STRICTLY READ-ONLY, safe on production
    uv run python scripts/provision_db_roles.py --verify

    # regenerate the manifest skeleton from a freshly migrated catalog
    uv run python scripts/provision_db_roles.py --emit-manifest

    # assert the Railway deployment's DSN custody (variable NAMES only)
    uv run python scripts/provision_db_roles.py --check-deployment-config

``--verify`` IS READ-ONLY BY CONSTRUCTION, NOT BY CONVENTION
------------------------------------------------------------
Three independent mechanisms, because "I only wrote SELECTs" is exactly
the claim that is worth not having to trust when the target is
production:

1. :func:`read_only_engine` connects with
   ``options=-c default_transaction_read_only=on``. The **server**
   rejects any INSERT/UPDATE/DELETE/DDL in that session with
   ``ERROR: cannot execute ... in a read-only transaction`` —
   independently of anything this file does.
2. :func:`_ro` asserts every statement string it executes begins with
   ``SELECT``/``WITH``/``SHOW`` before sending it, and it is the only
   execute path the verify code has.
3. :func:`verify` asserts ``SHOW transaction_read_only`` is ``on`` as
   its very first check, so a connection that somehow lost the setting
   fails the run instead of quietly proceeding.

:func:`provision` is the only writing function in this module, it is
never called from the verify path, and it refuses to run without an
explicit ``--provision``.

DSN CUSTODY USES THE EXISTING CONFIGURATION SCHEMA
--------------------------------------------------
No new environment variable is invented. The system (BYPASSRLS) DSN is
``SYSTEM_DATABASE_URL`` with its documented ``AUTH_DATABASE_URL``
fallback (``libs/shared/app_shared/config.py``, ``get_system_engine``).
:func:`check_deployment_config` reads each Railway service's variable
**names** — never their values — and asserts the system DSN is absent
from the public API service's environment. Process separation, not
env-var hopefulness: a variable that is not in the API container cannot
be reached by a bug in the API.

Exit codes: ``0`` clean, ``1`` at least one FAIL finding, ``2`` could not
run (no DSN, connection refused, manifest missing).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CANNOT_RUN = 2

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = REPO_ROOT / "scripts" / "provision_db_roles.sql"
MANIFEST_PATH = REPO_ROOT / "scripts" / "rls_table_manifest.txt"

APP_ROLE = "crawmatic_app"
#: The READY-007 plan's `crawmatic_system`. Production has run this role
#: under the name `crawmatic_auth` since the 2026-08-15 RLS cutover and
#: every service DSN authenticates as it; see provision_db_roles.sql's
#: header for why the deployed name is kept rather than duplicated.
SYSTEM_ROLE = "crawmatic_auth"
#: EPA A3/F03: the scrapyd services' narrow ingestion role. See
#: provision_db_roles.sql section 3a and scripts/sql/grants_expected.yaml.
SCRAPER_ROLE = "crawmatic_scraper"
MIGRATE_ROLE = "crawmatic_migrate"

#: Roles a RUNNING SERVICE authenticates as. Nothing in `public` may be
#: owned by one of these: an owner can `ALTER TABLE ... NO FORCE ROW
#: LEVEL SECURITY` and `DROP POLICY`, so an owner that is also a live
#: login can switch its own isolation off.
RUNTIME_LOGIN_ROLES = (APP_ROLE, SYSTEM_ROLE, SCRAPER_ROLE)

#: Manifest classes. See scripts/rls_table_manifest.txt's header.
CLASS_WORKSPACE = "WORKSPACE"
CLASS_TRANSITIVE = "TRANSITIVE"
CLASS_SYSTEM = "SYSTEM"
CLASS_TENANT_ROOT = "TENANT_ROOT"
CLASS_GAP = "GAP"

#: Classes whose relations must carry ENABLE + FORCE row level security
#: and at least one policy, on the parent AND on every partition of it.
TENANT_CLASSES = (CLASS_WORKSPACE, CLASS_TRANSITIVE)

VALID_CLASSES = (
    CLASS_WORKSPACE,
    CLASS_TRANSITIVE,
    CLASS_SYSTEM,
    CLASS_TENANT_ROOT,
    CLASS_GAP,
)

#: Railway services whose environment may legitimately hold a BYPASSRLS
#: DSN, and why. Consulted by :func:`check_deployment_config`.
SYSTEM_DSN_VARS = ("SYSTEM_DATABASE_URL", "AUTH_DATABASE_URL")
API_SERVICE = "api"
#: `AUTH_DATABASE_URL` in the API service is NOT a finding: the pre-auth
#: credential lookup (`app_shared.database.get_auth_session`) is
#: structurally cross-tenant — a login resolves a user by email before
#: any `app.workspace_id` exists to scope it with, and under FORCE RLS
#: the ordinary role returns zero rows for that query. The API needs it.
#: `SYSTEM_DATABASE_URL` is a different matter: nothing under `apps/api`
#: calls `get_system_session`, so its presence there would be pure
#: standing privilege.
API_FORBIDDEN_DSN_VARS = ("SYSTEM_DATABASE_URL",)


# =====================================================================
# Findings
# =====================================================================


@dataclass(frozen=True)
class Finding:
    """One verification outcome. ``FAIL`` decides the exit code."""

    severity: str  # "FAIL" | "WARN" | "INFO"
    check: str
    detail: str

    def render(self) -> str:
        return f"  {self.severity:<4}  {self.check} — {self.detail}"


def fail(check: str, detail: str) -> Finding:
    return Finding("FAIL", check, detail)


def warn(check: str, detail: str) -> Finding:
    return Finding("WARN", check, detail)


def info(check: str, detail: str) -> Finding:
    return Finding("INFO", check, detail)


# =====================================================================
# Manifest
# =====================================================================


@dataclass(frozen=True)
class ManifestEntry:
    table: str
    table_class: str
    note: str


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, ManifestEntry]:
    """Parse the reviewed inventory.

    Format: ``<table><TAB><class><TAB><note>``; ``#`` comments and blank
    lines ignored. Partition CHILDREN are deliberately not listed — they
    are derived from ``pg_inherits`` at verify time, so the manifest does
    not need a new line every month and stays identical across databases
    that have materialised different numbers of monthly partitions.
    """
    if not path.is_file():
        raise FileNotFoundError(f"reviewed manifest not found: {path}")

    entries: dict[str, ManifestEntry] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("\t") if p.strip()]
        if len(parts) < 3:
            raise ValueError(f"{path}:{lineno}: expected <table>TAB<class>TAB<note>, got {raw!r}")
        table, table_class, note = parts[0], parts[1], "\t".join(parts[2:])
        if table_class not in VALID_CLASSES:
            raise ValueError(
                f"{path}:{lineno}: unknown class {table_class!r} "
                f"(expected one of {', '.join(VALID_CLASSES)})"
            )
        if table in entries:
            raise ValueError(f"{path}:{lineno}: duplicate entry for {table!r}")
        entries[table] = ManifestEntry(table, table_class, note)
    return entries


# =====================================================================
# Catalog
# =====================================================================


@dataclass(frozen=True)
class Relation:
    name: str
    kind: str  # 'r' | 'p'
    is_partition: bool
    parent: str | None
    owner: str
    rls: bool
    forced: bool
    policies: int
    has_workspace_id: bool

    @property
    def enforced(self) -> bool:
        return self.rls and self.forced and self.policies > 0


_CATALOG_SQL = """
SELECT c.relname                                       AS name,
       c.relkind                                       AS kind,
       c.relispartition                                AS is_partition,
       parent.relname                                  AS parent,
       owner.rolname                                   AS owner,
       c.relrowsecurity                                AS rls,
       c.relforcerowsecurity                           AS forced,
       (SELECT count(*) FROM pg_policy p
         WHERE p.polrelid = c.oid)                     AS policies,
       EXISTS (SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.oid
                  AND a.attname = 'workspace_id'
                  AND NOT a.attisdropped)              AS has_workspace_id
FROM pg_class c
JOIN pg_roles owner ON owner.oid = c.relowner
LEFT JOIN pg_inherits inh ON inh.inhrelid = c.oid
LEFT JOIN pg_class parent ON parent.oid = inh.inhparent
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""

_READ_ONLY_PREFIXES = ("SELECT", "WITH", "SHOW", "TABLE")


def _ro(conn: Connection, sql: str, **params: object):
    """Execute a statement asserted to be read-only.

    The server-side ``default_transaction_read_only`` is the real
    guarantee (see the module docstring); this is the second, local one,
    so a future edit that pastes a write into the verify path fails in
    the developer's terminal rather than on production.
    """
    stripped = sql.strip().lstrip("(").lstrip()
    head = stripped.split(None, 1)[0].upper() if stripped else ""
    if head not in _READ_ONLY_PREFIXES:
        raise AssertionError(
            f"provision_db_roles: refusing to execute a non-read-only statement "
            f"on the verify path (starts with {head!r})"
        )
    return conn.execute(text(sql), params or {})


def read_catalog(conn: Connection) -> dict[str, Relation]:
    rows = _ro(conn, _CATALOG_SQL).all()
    return {
        row.name: Relation(
            name=row.name,
            kind=row.kind,
            is_partition=row.is_partition,
            parent=row.parent,
            owner=row.owner,
            rls=row.rls,
            forced=row.forced,
            policies=int(row.policies),
            has_workspace_id=row.has_workspace_id,
        )
        for row in rows
    }


def classify(rel: Relation) -> str:
    """The class the CATALOG implies for a relation, used by --emit-manifest.

    Deliberately mechanical: it reads ``workspace_id``/``relrowsecurity``
    and nothing else. Turning the result into a *reviewed* inventory —
    deciding whether a ``SYSTEM`` classification is genuinely
    tenant-free or is an unnoticed leak — is the human step the manifest
    file exists to record.
    """
    if rel.name == "workspaces":
        return CLASS_TENANT_ROOT
    if rel.has_workspace_id:
        return CLASS_WORKSPACE
    if rel.rls:
        return CLASS_TRANSITIVE
    return CLASS_SYSTEM


# =====================================================================
# Verify (READ-ONLY)
# =====================================================================


def read_only_engine(url: str) -> Engine:
    """An engine whose SERVER-SIDE default transaction mode is read-only."""
    return create_engine(
        url,
        connect_args={
            # PgBouncer transaction pooling: no server-side prepared
            # statements (same constraint as app_shared.database).
            "prepare_threshold": None,
            # The structural guarantee. Postgres itself refuses writes.
            "options": "-c default_transaction_read_only=on",
        },
        pool_pre_ping=True,
    )


def _check_read_only(conn: Connection) -> list[Finding]:
    mode = _ro(conn, "SHOW transaction_read_only").scalar_one()
    if str(mode).lower() != "on":
        return [
            fail(
                "verify session is read-only",
                f"transaction_read_only={mode!r} — refusing to continue; this "
                "session could write to the target database",
            )
        ]
    return [info("verify session is read-only", "server-enforced (default_transaction_read_only=on)")]


def _check_roles(conn: Connection) -> list[Finding]:
    """Role existence + attributes against the three-role model."""
    findings: list[Finding] = []
    rows = _ro(
        conn,
        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, rolcreaterole "
        "FROM pg_roles WHERE rolname = ANY(:names)",
        names=list((APP_ROLE, SYSTEM_ROLE, SCRAPER_ROLE, MIGRATE_ROLE)),
    ).all()
    found = {row.rolname: row for row in rows}

    expectations = {
        APP_ROLE: {
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcanlogin": True,
            "rolcreaterole": False,
        },
        SYSTEM_ROLE: {
            "rolsuper": False,
            # BYPASSRLS is intentional here and ONLY here.
            "rolbypassrls": True,
            "rolcanlogin": True,
            "rolcreaterole": False,
        },
        SCRAPER_ROLE: {
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcanlogin": True,
            "rolcreaterole": False,
        },
        MIGRATE_ROLE: {
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcanlogin": True,
            "rolcreaterole": False,
        },
    }

    for role, expected in expectations.items():
        row = found.get(role)
        if row is None:
            findings.append(
                fail(
                    f"role {role} exists",
                    "not present in pg_roles — this database has not been provisioned "
                    "by scripts/provision_db_roles.sql",
                )
            )
            continue
        actual = {attr: bool(getattr(row, attr)) for attr in expected}
        wrong = {a: (actual[a], expected[a]) for a in expected if actual[a] != expected[a]}
        if wrong:
            findings.append(
                fail(
                    f"role {role} attributes",
                    ", ".join(f"{a}={got} (expected {want})" for a, (got, want) in wrong.items()),
                )
            )
        else:
            findings.append(
                info(
                    f"role {role} attributes",
                    ", ".join(f"{a}={actual[a]}" for a in sorted(actual)),
                )
            )
    return findings


def _check_ownership(catalog: dict[str, Relation]) -> list[Finding]:
    """No relation in `public` may be owned by a role a service logs in as."""
    findings: list[Finding] = []
    owned_by_runtime: dict[str, list[str]] = {}
    for rel in catalog.values():
        if rel.owner in RUNTIME_LOGIN_ROLES:
            owned_by_runtime.setdefault(rel.owner, []).append(rel.name)

    for role, tables in sorted(owned_by_runtime.items()):
        findings.append(
            fail(
                "runtime roles own no tables",
                f"{role} owns {len(tables)} relation(s) in public — an owner can "
                f"ALTER TABLE ... NO FORCE ROW LEVEL SECURITY and DROP POLICY, i.e. "
                f"switch off its own isolation: {', '.join(sorted(tables)[:8])}"
                + (" ..." if len(tables) > 8 else "")
            )
        )
    if not owned_by_runtime:
        findings.append(info("runtime roles own no tables", "0 relations owned by a service login"))

    owners: dict[str, int] = {}
    for rel in catalog.values():
        owners[rel.owner] = owners.get(rel.owner, 0) + 1
    findings.append(
        info(
            "relation ownership",
            ", ".join(f"{owner}={count}" for owner, count in sorted(owners.items())),
        )
    )
    if MIGRATE_ROLE in owners and len(owners) > 1:
        strays = {o: c for o, c in owners.items() if o != MIGRATE_ROLE}
        findings.append(
            warn(
                "ownership consolidated on crawmatic_migrate",
                f"{sum(strays.values())} relation(s) still owned by "
                f"{', '.join(sorted(strays))} — run provisioning with "
                "provision_db_roles.adopt_ownership = on",
            )
        )
    return findings


def _check_manifest(
    manifest: dict[str, ManifestEntry], catalog: dict[str, Relation]
) -> list[Finding]:
    """Diff the reviewed inventory against catalog reality, both ways."""
    findings: list[Finding] = []
    parents = {name: rel for name, rel in catalog.items() if not rel.is_partition}

    # 1. A relation in the catalog that nobody reviewed is a FAIL: it is
    #    a table holding unknown data with unknown isolation.
    unreviewed = sorted(set(parents) - set(manifest))
    if unreviewed:
        findings.append(
            fail(
                "every catalog relation is in the reviewed manifest",
                f"{len(unreviewed)} unreviewed relation(s): {', '.join(unreviewed)} — "
                "add them to scripts/rls_table_manifest.txt with an annotation",
            )
        )
    else:
        findings.append(
            info(
                "every catalog relation is in the reviewed manifest",
                f"{len(parents)} non-partition relation(s) all annotated",
            )
        )

    # 2. A manifest entry with no catalog relation is a WARN, not a FAIL:
    #    the usual cause is a database behind the manifest's lane head
    #    (production lags the branch), which is drift to report, not a
    #    security defect.
    missing = sorted(set(manifest) - set(parents))
    if missing:
        findings.append(
            warn(
                "manifest relations exist in the catalog",
                f"{len(missing)} manifest relation(s) absent — this database is "
                f"probably behind the manifest's migration head: {', '.join(missing)}",
            )
        )

    # 3. The declared class must match what the catalog actually shows.
    for name in sorted(set(manifest) & set(parents)):
        entry = manifest[name]
        rel = parents[name]
        implied = classify(rel)
        if entry.table_class == CLASS_GAP:
            # A GAP is a KNOWN, ANNOTATED hole. Verify that it is still
            # exactly that: if RLS appeared, the manifest is stale and
            # must be re-reviewed (the annotation now lies).
            if rel.rls:
                findings.append(
                    warn(
                        f"manifest class for {name}",
                        "declared GAP but the catalog now shows row-level security — "
                        "the gap was closed; reclassify it in the manifest",
                    )
                )
            else:
                findings.append(
                    warn(
                        f"known isolation gap: {name}",
                        f"{entry.note}",
                    )
                )
            continue
        if entry.table_class != implied:
            findings.append(
                fail(
                    f"manifest class for {name}",
                    f"declared {entry.table_class} but the catalog implies {implied} "
                    f"(workspace_id={rel.has_workspace_id}, rls={rel.rls})",
                )
            )

    # 4. Every tenant-class relation must actually enforce, parent and
    #    partitions alike. A partition is a separate relation with its
    #    own policies: `SELECT * FROM request_attempts_2026_08` is
    #    checked against the CHILD's policies, not the parent's.
    unenforced: list[str] = []
    checked = 0
    for name, entry in sorted(manifest.items()):
        if entry.table_class not in TENANT_CLASSES:
            continue
        rel = parents.get(name)
        if rel is None:
            continue
        family = [rel] + [c for c in catalog.values() if c.parent == name]
        for member in family:
            checked += 1
            if not member.enforced:
                unenforced.append(
                    f"{member.name}(rls={member.rls}, forced={member.forced}, "
                    f"policies={member.policies})"
                )
    if unenforced:
        findings.append(
            fail(
                "ENABLE + FORCE + >=1 policy on every tenant relation",
                f"{len(unenforced)} relation(s) do not enforce: {', '.join(unenforced)}",
            )
        )
    else:
        findings.append(
            info(
                "ENABLE + FORCE + >=1 policy on every tenant relation",
                f"{checked} relation(s) (manifest parents + their partitions)",
            )
        )

    # 5. A relation the manifest calls SYSTEM/TENANT_ROOT must not have
    #    grown a workspace_id column without being reclassified.
    for name, entry in sorted(manifest.items()):
        rel = parents.get(name)
        if rel is None or entry.table_class in TENANT_CLASSES:
            continue
        if rel.has_workspace_id:
            findings.append(
                fail(
                    f"manifest class for {name}",
                    f"declared {entry.table_class} but now carries a workspace_id "
                    "column — it holds tenant data and needs RLS",
                )
            )
    return findings


def _check_alembic_head(conn: Connection) -> list[Finding]:
    try:
        heads = [r[0] for r in _ro(conn, "SELECT version_num FROM alembic_version")]
    except Exception as exc:  # pragma: no cover - permission/absence dependent
        # A failed statement leaves the transaction aborted, and every
        # later query on this connection would then fail with
        # `InFailedSqlTransaction` — reporting a database with no schema
        # as a connection error rather than as an unmigrated database.
        # Roll back so the remaining checks still run and say something
        # true.
        conn.rollback()
        return [warn("alembic head readable", f"{type(exc).__name__}: {exc}")]
    return [info("alembic head", ", ".join(heads) or "(none)")]


def verify(url: str, manifest_path: Path = MANIFEST_PATH) -> list[Finding]:
    """Diff catalog reality against the reviewed manifest and the role model.

    Performs ZERO writes — see the module docstring for the three
    independent mechanisms that make that structural rather than
    aspirational.
    """
    manifest = load_manifest(manifest_path)
    findings: list[Finding] = []
    engine = read_only_engine(url)
    try:
        with engine.connect() as conn:
            findings.extend(_check_read_only(conn))
            if any(f.severity == "FAIL" for f in findings):
                return findings
            findings.extend(_check_alembic_head(conn))
            findings.extend(_check_roles(conn))
            catalog = read_catalog(conn)
            findings.extend(_check_ownership(catalog))
            findings.extend(_check_manifest(manifest, catalog))
    finally:
        engine.dispose()
    return findings


# =====================================================================
# Provision (WRITES — never reachable from the verify path)
# =====================================================================


def load_sql(path: Path = SQL_PATH) -> str:
    return path.read_text(encoding="utf-8")


def provision(
    url: str,
    *,
    app_password: str | None = None,
    auth_password: str | None = None,
    scraper_password: str | None = None,
    migrate_password: str | None = None,
    adopt_ownership: bool = False,
) -> None:
    """Execute ``scripts/provision_db_roles.sql`` idempotently.

    Requires a privileged (owner/superuser) DSN — creating roles needs
    CREATEROLE and adopting ownership needs membership of the target
    role. Passwords travel in transaction-local GUCs so they cannot
    outlive the statement that consumed them; omitting one leaves that
    role's existing password untouched.
    """
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for guc, value in (
                ("provision_db_roles.app_password", app_password),
                ("provision_db_roles.auth_password", auth_password),
                ("provision_db_roles.scraper_password", scraper_password),
                ("provision_db_roles.migrate_password", migrate_password),
                ("provision_db_roles.adopt_ownership", "on" if adopt_ownership else "off"),
            ):
                conn.execute(
                    text("SELECT set_config(:guc, :val, true)"), {"guc": guc, "val": value}
                )

            # The DBAPI cursor directly, with NO parameter sequence:
            # psycopg3 only scans a query for `%s` placeholders when
            # parameters are supplied, and the SQL body contains
            # `format(..., %L)` / `%I` inside its DO blocks. Passing even
            # an empty tuple triggers that scan and fails.
            cursor = conn.connection.cursor()
            try:
                cursor.execute(load_sql())
            finally:
                cursor.close()

            # Partitions do not inherit their parent's policies. Rather
            # than restate the inheritance DDL here (a second definition
            # that could drift), run the single source of truth the
            # migration and the runtime partition-creation job both use.
            from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL

            cursor = conn.connection.cursor()
            try:
                cursor.execute(PARTITION_RLS_INHERITANCE_SQL)
            finally:
                cursor.close()
    finally:
        engine.dispose()


# =====================================================================
# Manifest emission
# =====================================================================

_MANIFEST_HEADER = """\
# scripts/rls_table_manifest.txt — the REVIEWED row-level-security
# inventory (READY-007 / P0.5).
#
# Format: <table><TAB><class><TAB><annotation>
#
# This file is the human half of the isolation contract.
# `scripts/provision_db_roles.py --verify` reads the DATABASE CATALOG
# (pg_class.relrowsecurity / pg_policy / the workspace_id column) and
# diffs it against these declarations in BOTH directions:
#
#   * a catalog relation missing from this file FAILS the check — an
#     unreviewed table is a table whose isolation nobody looked at;
#   * a declaration that no longer matches the catalog FAILS — so a
#     migration that drops a policy cannot pass by leaving this file
#     alone.
#
# The inventory is never derived by walking Python model subclasses: a
# table can exist in the database without a mapped class (partitions,
# `alembic_version`, anything created by hand during an incident), and
# those are exactly the relations that go unnoticed.
#
# PARTITION CHILDREN ARE NOT LISTED. They are derived from pg_inherits
# at verify time and inherit their parent's class, so this file needs no
# new line each month and reads identically against databases that have
# materialised different numbers of monthly partitions.
#
# CLASSES
# -------
#   WORKSPACE    Carries a `workspace_id` column: one workspace owns each
#                row. Must have ENABLE + FORCE ROW LEVEL SECURITY and at
#                least one policy, on the parent and on every partition.
#
#   TRANSITIVE   Holds tenant data but has NO `workspace_id` column of
#                its own. Scoped through a parent row by an `EXISTS`
#                policy. The annotation names the parent and the join
#                column, because that indirection is the whole reason
#                the table looks unscoped in a column-based audit.
#
#   SYSTEM       No tenant data. RLS deliberately absent. The annotation
#                must say WHY the table is tenant-free — this is the
#                claim a security reviewer is actually checking.
#
#   TENANT_ROOT  The workspace registry itself.
#
#   GAP          Holds tenant-linked data with NO row-level security.
#                A KNOWN, ANNOTATED hole, not an exclusion. --verify
#                reports every GAP on every run so it cannot go quiet,
#                and flags it if RLS later appears (the annotation would
#                then be stale).
#
"""


def emit_manifest(url: str) -> str:
    """Render a manifest skeleton from the catalog, for human annotation."""
    engine = read_only_engine(url)
    try:
        with engine.connect() as conn:
            catalog = read_catalog(conn)
    finally:
        engine.dispose()

    existing = load_manifest() if MANIFEST_PATH.is_file() else {}
    lines = [_MANIFEST_HEADER]
    parents = {n: r for n, r in catalog.items() if not r.is_partition}
    by_class: dict[str, list[str]] = {}
    for name, rel in sorted(parents.items()):
        prior = existing.get(name)
        table_class = prior.table_class if prior else classify(rel)
        note = prior.note if prior else "TODO: annotate"
        by_class.setdefault(table_class, []).append(f"{name}\t{table_class}\t{note}")
    for table_class in VALID_CLASSES:
        rows = by_class.get(table_class)
        if not rows:
            continue
        lines.append(f"# --- {table_class} ---")
        lines.extend(rows)
        lines.append("")
    return "\n".join(lines)


# =====================================================================
# Deployment configuration assertion (Railway service variable NAMES)
# =====================================================================


def _railway_variable_names(service: str, project_dir: Path) -> list[str]:
    """Variable NAMES for one Railway service. Values are never returned.

    The CLI is invoked with ``--json`` and only ``dict.keys()`` leaves
    this function, so no secret can reach a log, a report, or a terminal
    through this path.
    """
    result = subprocess.run(
        ["railway", "variables", "--service", service, "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`railway variables --service {service}` exited {result.returncode} "
            "(is this directory linked to the project, and is RAILWAY_API_TOKEN set?)"
        )
    return sorted(json.loads(result.stdout).keys())


def check_deployment_config(
    project_dir: Path,
    services: tuple[str, ...] = ("api", "worker", "scheduler", "migrate"),
) -> list[Finding]:
    """Assert DSN custody across the deployed service environments.

    The rule this enforces is process separation: the system (BYPASSRLS)
    DSN must not exist in the public API service's environment at all. A
    variable that is not in the container cannot be reached by a bug, a
    dependency, or a future refactor in that container — which is a
    stronger statement than any amount of "no caller does that today".
    """
    findings: list[Finding] = []
    for service in services:
        try:
            names = _railway_variable_names(service, project_dir)
        except Exception as exc:
            findings.append(warn(f"read {service} variables", f"{type(exc).__name__}: {exc}"))
            continue

        dsn_vars = [n for n in names if n in SYSTEM_DSN_VARS or n.endswith("DATABASE_URL")]
        findings.append(info(f"{service} DSN variables", ", ".join(dsn_vars) or "(none)"))

        if service == API_SERVICE:
            offenders = [n for n in names if n in API_FORBIDDEN_DSN_VARS]
            if offenders:
                findings.append(
                    fail(
                        "system DSN absent from the API service",
                        f"the public API service environment defines {', '.join(offenders)} — "
                        "the BYPASSRLS system DSN belongs only in maintenance/worker "
                        "service environments",
                    )
                )
            else:
                findings.append(
                    info(
                        "system DSN absent from the API service",
                        f"none of {', '.join(API_FORBIDDEN_DSN_VARS)} present",
                    )
                )
            if "AUTH_DATABASE_URL" in names:
                findings.append(
                    warn(
                        "API holds the pre-auth BYPASSRLS DSN",
                        "AUTH_DATABASE_URL is present in the API service. This is the "
                        "sanctioned pre-auth credential-lookup seam "
                        "(app_shared.database.get_auth_session) and the API cannot "
                        "resolve a login without it — recorded here so the standing "
                        "BYPASSRLS privilege stays visible to review, not because it "
                        "is a defect",
                    )
                )
    return findings


# =====================================================================
# CLI
# =====================================================================


def resolve_url(args: argparse.Namespace) -> str | None:
    """The DSN to act on.

    ``--dsn-stdin`` exists so an operator can pipe a production DSN
    straight out of a secret store without it ever being written to
    disk, to a shell history, or to an environment listing.
    """
    if args.dsn_stdin:
        return sys.stdin.read().strip() or None
    return (
        os.environ.get("PROVISION_DB_ROLES_URL")
        or os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )


def _sanitized_target(url: str) -> str:
    parsed = make_url(url)
    return f"user={parsed.username} host={parsed.host} port={parsed.port} db={parsed.database}"


def render(findings: list[Finding], title: str) -> str:
    out = ["=" * 72, title, "=" * 72]
    out.extend(f.render() for f in findings)
    failures = [f for f in findings if f.severity == "FAIL"]
    warnings = [f for f in findings if f.severity == "WARN"]
    out.append("=" * 72)
    if failures:
        out.append(f"RESULT: FAILED — {len(failures)} FAIL, {len(warnings)} WARN")
    else:
        out.append(f"RESULT: PASSED — 0 FAIL, {len(warnings)} WARN")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify",
        action="store_true",
        help="read-only: diff catalog + role attributes against the reviewed manifest",
    )
    mode.add_argument(
        "--provision", action="store_true", help="WRITES: create/repair roles, grants and FORCE RLS"
    )
    mode.add_argument(
        "--emit-manifest",
        action="store_true",
        help="render a manifest skeleton from the catalog to stdout (read-only)",
    )
    mode.add_argument(
        "--check-deployment-config",
        action="store_true",
        help="assert DSN custody across Railway service environments (names only)",
    )
    parser.add_argument(
        "--dsn-stdin", action="store_true", help="read the DSN from stdin instead of the environment"
    )
    parser.add_argument(
        "--adopt-ownership",
        action="store_true",
        help="--provision only: also reassign relation ownership to crawmatic_migrate",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=REPO_ROOT,
        help="--check-deployment-config only: the Railway-linked directory",
    )
    args = parser.parse_args(argv)

    if args.check_deployment_config:
        findings = check_deployment_config(args.project_dir)
        print(render(findings, "DEPLOYMENT DSN CUSTODY (variable names only)"))
        return EXIT_FAILED if any(f.severity == "FAIL" for f in findings) else EXIT_OK

    url = resolve_url(args)
    if not url:
        print(
            "provision_db_roles: no DSN. Set PROVISION_DB_ROLES_URL (or "
            "MIGRATION_DATABASE_URL / DATABASE_URL), or pass --dsn-stdin and pipe one in.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    try:
        if args.provision:
            provision(
                url,
                app_password=os.environ.get("CRAWMATIC_APP_DB_PASSWORD") or None,
                auth_password=os.environ.get("CRAWMATIC_AUTH_DB_PASSWORD") or None,
                scraper_password=os.environ.get("CRAWMATIC_SCRAPER_DB_PASSWORD") or None,
                migrate_password=os.environ.get("CRAWMATIC_MIGRATE_DB_PASSWORD") or None,
                adopt_ownership=args.adopt_ownership,
            )
            print(f"provisioned roles on {_sanitized_target(url)}")
            findings = verify(url)
        elif args.emit_manifest:
            print(emit_manifest(url))
            return EXIT_OK
        else:
            findings = verify(url)
    except FileNotFoundError as exc:
        print(f"provision_db_roles: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    except Exception as exc:
        print(
            f"provision_db_roles: could not run against {_sanitized_target(url)} — "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    print(render(findings, f"DATABASE ROLE + RLS POSTURE — {_sanitized_target(url)}"))
    return EXIT_FAILED if any(f.severity == "FAIL" for f in findings) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
