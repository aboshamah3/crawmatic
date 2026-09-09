#!/usr/bin/env python3
"""Diff a live database's actual table grants against the reviewed
component-scoped manifest (EPA A3/F03).

Companion to ``scripts/sql/grants_expected.yaml`` (the reviewed
per-role/per-table privilege manifest) and ``scripts/provision_db_roles.
sql`` (the DDL that applies those exact grants). Where
``provision_db_roles.py --verify`` checks role EXISTENCE/attributes and
RLS posture, this script checks the actual PRIVILEGE GRANTS a role holds
on each table — the other half of "component-scoped credentials": a role
can be correctly BYPASSRLS-or-not and still hold a blanket ``GRANT ...
ON ALL TABLES`` nobody reviewed.

    # verify an existing database — STRICTLY READ-ONLY, safe on production
    VERIFY_GRANTS_URL=postgresql+psycopg://owner:pw@127.0.0.1:5432/crawmatic \\
        uv run python scripts/verify_grants.py

Read-only by the same three-mechanism construction as
``provision_db_roles.py --verify`` (see that module's docstring):
1. the connection sets ``default_transaction_read_only=on`` server-side;
2. every statement this file executes is asserted to start with a
   read-only keyword before being sent;
3. ``SHOW transaction_read_only`` is checked first and the run aborts if
   it is not ``on``.

Method: for each ``(role, table)`` pair in the manifest, query
``information_schema.role_table_grants`` for the privileges that role
actually holds on that table (grantee is the role itself OR PUBLIC,
matching how Postgres privilege checks work), and diff the resulting set
against the manifest's expected set. Three kinds of drift:

* **missing**   — the manifest expects a privilege the role does not have
  (a regression: something that used to work may now silently fail, or a
  provisioning step was never run).
* **extra**     — the role holds a privilege the manifest does not name
  (a blast-radius regression: broader access than the reviewed set).
* **unreviewed table** — a table exists in the live catalog that has no
  entry for a role in the manifest at all. This mirrors
  ``rls_table_manifest.txt``'s own contract (an unreviewed table is a
  table whose isolation nobody looked at) but is reported as a WARNING,
  not a FAIL: a table entirely absent from the manifest is caught by
  ``tests/unit/test_grants_manifest_schema.py`` against
  ``rls_table_manifest.txt`` already, and a live catalog can legitimately
  contain relations the manifest does not track privileges for at all
  (partition children, which inherit their parent's grants and are never
  granted individually).

Exit codes: ``0`` clean (no drift), ``2`` drift found, ``2`` also for
"could not run" (no DSN, connection refused, manifest missing) — a
verifier that cannot run is not a clean pass and must not report ``0``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

EXIT_OK = 0
EXIT_DRIFT = 2
EXIT_CANNOT_RUN = 2

REPO_ROOT = Path(__file__).resolve().parents[1]
GRANTS_PATH = REPO_ROOT / "scripts" / "sql" / "grants_expected.yaml"

_READ_ONLY_PREFIXES = ("SELECT", "WITH", "SHOW", "TABLE")

_GRANTS_SQL = """
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND grantee = ANY(:roles)
"""


@dataclass(frozen=True)
class Drift:
    role: str
    table: str
    kind: str  # "missing" | "extra"
    privilege: str

    def render(self) -> str:
        return f"[{self.kind.upper()}] {self.role}.{self.table}: {self.privilege}"


def load_manifest(path: Path = GRANTS_PATH) -> dict[str, dict[str, set[str]]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping of role -> table -> privileges")
    manifest: dict[str, dict[str, set[str]]] = {}
    for role, tables in raw.items():
        if not isinstance(tables, dict):
            raise ValueError(f"{path}: role {role!r} must map to a table -> privileges mapping")
        manifest[role] = {table: set(privs or []) for table, privs in tables.items()}
    return manifest


def _ro(conn: Connection, sql: str, **params: object):
    """Execute a statement asserted to be read-only (mirrors
    ``provision_db_roles.py``'s ``_ro``)."""
    stripped = sql.strip().lstrip("(").lstrip()
    head = stripped.split(None, 1)[0].upper() if stripped else ""
    if head not in _READ_ONLY_PREFIXES:
        raise AssertionError(
            f"verify_grants: refusing to execute a non-read-only statement "
            f"(starts with {head!r})"
        )
    return conn.execute(text(sql), params or {})


def read_only_engine(url: str) -> Engine:
    return create_engine(
        url,
        connect_args={
            "prepare_threshold": None,
            "options": "-c default_transaction_read_only=on",
        },
        pool_pre_ping=True,
    )


def read_actual_grants(conn: Connection, roles: list[str]) -> dict[str, dict[str, set[str]]]:
    """``{role: {table: {privileges held}}}`` from the live catalog."""
    rows = _ro(conn, _GRANTS_SQL, roles=roles).all()
    actual: dict[str, dict[str, set[str]]] = {role: {} for role in roles}
    for row in rows:
        actual.setdefault(row.grantee, {}).setdefault(row.table_name, set()).add(
            row.privilege_type
        )
    return actual


def diff_grants(
    expected: dict[str, dict[str, set[str]]],
    actual: dict[str, dict[str, set[str]]],
) -> list[Drift]:
    drifts: list[Drift] = []
    for role, tables in expected.items():
        actual_tables = actual.get(role, {})
        for table, expected_privs in tables.items():
            actual_privs = actual_tables.get(table, set())
            for missing in sorted(expected_privs - actual_privs):
                drifts.append(Drift(role, table, "missing", missing))
            for extra in sorted(actual_privs - expected_privs):
                drifts.append(Drift(role, table, "extra", extra))
    return drifts


def unreviewed_tables(
    expected: dict[str, dict[str, set[str]]],
    actual: dict[str, dict[str, set[str]]],
) -> dict[str, list[str]]:
    """Tables a role holds SOME grant on that the manifest never mentions
    for that role at all (as opposed to mentioning with an empty list)."""
    findings: dict[str, list[str]] = {}
    for role, tables in actual.items():
        expected_tables = expected.get(role, {})
        extra_tables = sorted(t for t in tables if t not in expected_tables)
        if extra_tables:
            findings[role] = extra_tables
    return findings


def resolve_url() -> str | None:
    return (
        os.environ.get("VERIFY_GRANTS_URL")
        or os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )


def _sanitized_target(url: str) -> str:
    parsed = make_url(url)
    return f"user={parsed.username} host={parsed.host} port={parsed.port} db={parsed.database}"


def run(url: str, manifest_path: Path = GRANTS_PATH) -> int:
    expected = load_manifest(manifest_path)
    engine = read_only_engine(url)
    try:
        with engine.connect() as conn:
            mode = _ro(conn, "SHOW transaction_read_only").scalar_one()
            if str(mode).lower() != "on":
                print(
                    "verify_grants: FAIL — verify session is not read-only "
                    f"(transaction_read_only={mode!r}); refusing to continue",
                    file=sys.stderr,
                )
                return EXIT_CANNOT_RUN
            actual = read_actual_grants(conn, list(expected))
    finally:
        engine.dispose()

    drifts = diff_grants(expected, actual)
    unreviewed = unreviewed_tables(expected, actual)

    print(f"verify_grants: checked {len(expected)} role(s) against {_sanitized_target(url)}")
    for role, tables in expected.items():
        print(f"  {role}: {len(tables)} table(s) in manifest")

    if not drifts and not unreviewed:
        print("verify_grants: OK — no drift between the live grants and the manifest.")
        return EXIT_OK

    for drift in drifts:
        print(f"verify_grants: {drift.render()}", file=sys.stderr)
    for role, tables in sorted(unreviewed.items()):
        print(
            f"verify_grants: [WARN] {role} holds a grant on table(s) not in the "
            f"manifest at all: {', '.join(tables)}",
            file=sys.stderr,
        )

    if drifts:
        print(
            f"verify_grants: FAIL — {len(drifts)} drift finding(s) between the live "
            "database and scripts/sql/grants_expected.yaml",
            file=sys.stderr,
        )
        return EXIT_DRIFT
    # Only unreviewed-table warnings, no missing/extra privilege drift:
    # still a clean pass on the manifest's own terms, but printed loudly.
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--manifest",
        default=str(GRANTS_PATH),
        help="path to grants_expected.yaml",
    )
    parser.add_argument(
        "--dsn-stdin",
        action="store_true",
        help="read the target DSN from stdin instead of the environment",
    )
    args = parser.parse_args(argv)

    if args.dsn_stdin:
        url = sys.stdin.read().strip() or None
    else:
        url = resolve_url()
    if not url:
        print(
            "verify_grants: no database URL (set VERIFY_GRANTS_URL / "
            "MIGRATION_DATABASE_URL / DATABASE_URL, or pass --dsn-stdin)",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"verify_grants: manifest not found at {manifest_path}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    try:
        return run(url, manifest_path)
    except Exception as exc:  # noqa: BLE001 - fail closed with context, never a bare traceback exit
        print(f"verify_grants: could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
