#!/usr/bin/env python3
"""Emit the owner-run SQL that backfills ``workspaces.external_ref`` (security E5).

Read-only: reads a CSV of ``engine_workspace_id,project_id`` pairs and prints
``UPDATE`` statements to stdout. It never connects to any database.

The SaaS side holds the mapping: ``Project.cmWorkspaceId`` (the engine
workspace id) and ``Project.id`` (sent as ``external_ref`` by
``activationCore.ts``). Export it from the SaaS DB (owner-run, creds from the
environment), e.g.::

    psql "$SAAS_DATABASE_URL" -At -F, -c \
      'SELECT "cmWorkspaceId", id FROM "Project" WHERE "cmWorkspaceId" IS NOT NULL' \
      > map.csv
    python scripts/security/backfill_workspace_external_ref.py map.csv > backfill.sql
    psql "$ENGINE_DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f backfill.sql

Only NULL ``external_ref`` rows are touched, so re-running is safe.
"""
from __future__ import annotations

import csv
import sys
import uuid


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_statements(rows) -> list[str]:
    out: list[str] = []
    seen_ws: set[str] = set()
    seen_ref: set[str] = set()
    for row in rows:
        if len(row) != 2:
            raise ValueError(f"expected 2 columns, got {row!r}")
        ws, ref = row[0].strip(), row[1].strip()
        uuid.UUID(ws)  # raises on a malformed id
        if not ref:
            raise ValueError(f"empty project id for workspace {ws}")
        if ws in seen_ws or ref in seen_ref:
            raise ValueError(f"duplicate workspace id or ref in input: {ws} / {ref}")
        seen_ws.add(ws)
        seen_ref.add(ref)
        out.append(
            f"UPDATE workspaces SET external_ref = {_lit(ref)} "
            f"WHERE id = {_lit(ws)}::uuid AND external_ref IS NULL;"
        )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], newline="") as fh:
        stmts = build_statements(csv.reader(fh))
    print("BEGIN;")
    print("\n".join(stmts))
    print("COMMIT;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
