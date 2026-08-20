"""One-shot deploy step: provision the two runtime DB roles (audit B6).

    docker compose run --rm migrate python -m migrate.provision_roles
    # or, from a checkout:
    uv run python -m migrate.provision_roles

WHY THIS EXISTS
---------------
`scripts/rls_provision.sql` has been the source of truth for the role
model since the 2026-08-15 readiness audit, but it was only ever a
**manual** `psql` invocation: nothing in the repo ran it, no image
shipped a psql client, and the repo's own defaults still pointed
`DATABASE_URL` at the bootstrap/owner role. So the RLS posture that
production was provisioned with by hand could silently fail to exist in
any environment created afterwards — and a table owner (or a superuser)
reads every workspace's rows regardless of how many `CREATE POLICY`
statements the migrations emitted.

This module is the executable half. It runs in the SAME one-shot
migration image that runs `alembic upgrade head` (the repo's established
convention for a privileged, run-once database step: see
`contracts/migration-job.md` and `apps/migrate/Dockerfile`), and it
executes `scripts/sql/rls_roles.sql` — literally the same statements the
psql path runs, not a re-implementation.

WHAT IT GUARANTEES
------------------
Roles are cluster-level objects carrying passwords, so they deliberately
stay out of Alembic (see `scripts/rls_provision.sql`'s own header for
that reasoning — putting `CREATE ROLE` in a migration would make every
migration require CREATEROLE and would put a password in the migration
history). Instead this runs *beside* the migration, is idempotent, and
**verifies the result before exiting 0**: if `crawmatic_app` ends up
SUPERUSER or BYPASSRLS, or owns any table, it exits non-zero. A deploy
step that only issues DDL and never checks the outcome is how the
superuser-as-app-role hole survived three audits.

Since the 2026-08-20 security review it also verifies the isolation
posture itself, asked from the `workspace_id` **column**
(`workspace_scoped_rls_problems`): every relation in `public` carrying
that column must have RLS enabled, FORCEd, and at least one policy.
Read the previous way round — "of the tables where RLS is on, which lack
FORCE?" — the eight monthly partitions that had no RLS at all were not
even candidates for the check, which is why nothing here caught them.

ENVIRONMENT
-----------
    MIGRATION_DATABASE_URL   required — the owner/admin role, direct to
                             Postgres (never the PgBouncer pooler), the
                             same URL `alembic upgrade head` uses.
    CRAWMATIC_APP_DB_PASSWORD    optional — set the `crawmatic_app` password.
    CRAWMATIC_AUTH_DB_PASSWORD   optional — set the `crawmatic_auth` password.

Omitting a password leaves that role's existing password untouched (the
role is still created and its attributes still repaired), so re-running
this step during a deploy never rotates a credential by accident.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

#: `scripts/sql/rls_roles.sql`, relative to the repo root. Both the
#: migration image (`COPY . .` into /app) and a plain checkout have the
#: same layout, so one relative path resolves in both.
_SQL_RELATIVE_PATH = Path("scripts") / "sql" / "rls_roles.sql"

_APP_ROLE = "crawmatic_app"
_AUTH_ROLE = "crawmatic_auth"


def _repo_root() -> Path:
    """Walk up from this file until `scripts/sql/rls_roles.sql` is found."""
    for candidate in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (candidate / _SQL_RELATIVE_PATH).is_file():
            return candidate
    raise FileNotFoundError(
        f"could not locate {_SQL_RELATIVE_PATH} from {Path.cwd()} or "
        f"{Path(__file__).resolve()} — run this from the repo root or the "
        "migration image, both of which contain scripts/."
    )


def load_sql() -> str:
    """The shared role/GRANT body — the same statements `psql -f
    scripts/rls_provision.sql` executes, never a second copy of them."""
    return (_repo_root() / _SQL_RELATIVE_PATH).read_text(encoding="utf-8")


def provision(database_url: str, *, app_password: str | None, auth_password: str | None) -> None:
    """Create/repair both runtime roles and their grants, idempotently."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            # Transaction-local (`true`) so a password can never outlive
            # this transaction — the same mechanism the psql path uses.
            conn.execute(
                text("SELECT set_config('rls_provision.app_password', :pw, true)"),
                {"pw": app_password},
            )
            conn.execute(
                text("SELECT set_config('rls_provision.auth_password', :pw, true)"),
                {"pw": auth_password},
            )
            # The DBAPI cursor directly, with NO parameter sequence: psycopg3
            # only scans a query for `%s`-style placeholders when parameters
            # are supplied, and this body contains `format(..., %L)` /
            # `%I` inside its DO blocks. `exec_driver_sql` passes an empty
            # tuple, which is enough to trigger that scan and fail.
            cursor = conn.connection.cursor()
            try:
                cursor.execute(load_sql())
            finally:
                cursor.close()
    finally:
        engine.dispose()


#: Every relation in ``public`` that carries a ``workspace_id`` column,
#: with the three facts that decide whether that column is actually
#: enforced. Partitions are relkind ``'r'`` like any other table, so they
#: are candidates here by construction — which is the whole point.
_WORKSPACE_SCOPED_RLS_SQL = """
SELECT c.relname,
       c.relrowsecurity,
       c.relforcerowsecurity,
       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policy_count
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND EXISTS (
      SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = c.oid
        AND a.attname = 'workspace_id'
        AND NOT a.attisdropped
  )
ORDER BY c.relname
"""


def workspace_scoped_rls_problems(database_url: str) -> list[str]:
    """Return every relation that owns workspace data without enforcing it.

    **Read this the right way round** (security review A1, 2026-08-20).
    :func:`verify` used to ask only "of the tables where RLS is already
    ENABLED, which lack FORCE?" — a question a table with no RLS at all
    is not even a candidate for. Eight monthly partitions of
    ``request_attempts`` / ``price_observations`` / ``price_alert_events``
    / ``webhook_events`` each carried ``workspace_id`` and had no RLS
    whatsoever, and that check could not have found them: it was looking
    for a degraded posture, not an absent one.

    So the question is asked from the **column** instead. Anything in
    ``public`` with a ``workspace_id`` column holds one workspace's data
    per row, and therefore must have RLS enabled, FORCEd (so the table
    owner is not exempt), and at least one policy (RLS with no policy
    denies everything, which is fail-closed but is a different bug and
    should be reported as one). A partition is an ordinary table to this
    query, so a partition created next month is checked on the deploy
    after it appears — no list of table names to keep in step with the
    schema.

    The policy *content* is deliberately not asserted here: a strict
    workspace-scoped table and a dual-scope one
    (``emit_global_readable_rls_policy``) legitimately carry different
    predicates, and
    ``tests/integration/test_rls_cross_workspace.py`` is what proves the
    predicate actually confines a stranger. This function proves the
    control is switched on at all.
    """
    problems: list[str] = []
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            for relname, rls_on, forced, policy_count in conn.execute(
                text(_WORKSPACE_SCOPED_RLS_SQL)
            ):
                if not rls_on:
                    problems.append(
                        f"{relname!r} has a workspace_id column but NO row-level security "
                        "— every workspace's rows are readable by any role holding SELECT "
                        "on it (this is what the 2026-08-20 review found on the monthly "
                        "partitions)"
                    )
                    continue
                if not forced:
                    problems.append(
                        f"{relname!r} has row-level security without FORCE — the table "
                        "owner is exempt from its own policies"
                    )
                if policy_count == 0:
                    problems.append(
                        f"{relname!r} has row-level security enabled but no policy at all "
                        "— it denies every row to every non-owner, which is a failure, not "
                        "isolation"
                    )
    finally:
        engine.dispose()
    return problems


def verify(database_url: str) -> list[str]:
    """Return a list of posture violations — empty means the posture holds."""
    problems: list[str] = []
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin "
                    "FROM pg_roles WHERE rolname IN (:app, :auth)"
                ),
                {"app": _APP_ROLE, "auth": _AUTH_ROLE},
            ).all()
            found = {row[0]: row for row in rows}

            for role in (_APP_ROLE, _AUTH_ROLE):
                if role not in found:
                    problems.append(f"role {role!r} does not exist after provisioning")
                    continue
                _, is_super, bypass_rls, can_login = found[role]
                if is_super:
                    problems.append(f"role {role!r} is SUPERUSER — a superuser ignores RLS entirely")
                if not can_login:
                    problems.append(f"role {role!r} cannot LOGIN")
                if role == _APP_ROLE and bypass_rls:
                    problems.append(
                        f"role {role!r} has BYPASSRLS — the runtime role must NOT; "
                        "only crawmatic_auth may, for the pre-auth/admin/scheduler seams"
                    )
                if role == _AUTH_ROLE and not bypass_rls:
                    problems.append(
                        f"role {role!r} lacks BYPASSRLS — the pre-auth credential "
                        "lookup would silently return 0 rows under FORCE RLS"
                    )

            owned = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tableowner IN (:app, :auth)"
                ),
                {"app": _APP_ROLE, "auth": _AUTH_ROLE},
            ).scalars().all()
            if owned:
                problems.append(
                    "runtime roles own tables (owners bypass RLS unless FORCE is set on "
                    f"every one of them): {', '.join(sorted(owned))}"
                )

            unforced = conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "WHERE c.relnamespace = 'public'::regnamespace "
                    "AND c.relkind IN ('r', 'p') AND c.relrowsecurity "
                    "AND NOT c.relforcerowsecurity"
                )
            ).scalars().all()
            if unforced:
                problems.append(
                    "RLS-enabled tables missing FORCE ROW LEVEL SECURITY: "
                    f"{', '.join(sorted(unforced))}"
                )
    finally:
        engine.dispose()

    # Asked from the workspace_id COLUMN rather than from relrowsecurity,
    # so a relation with NO row-level security at all is a finding rather
    # than a non-candidate. See workspace_scoped_rls_problems.
    problems.extend(workspace_scoped_rls_problems(database_url))
    return problems


def main(argv: list[str] | None = None) -> int:
    database_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not database_url:
        print(
            "MIGRATION_DATABASE_URL is required (the owner/admin role, direct to "
            "Postgres — the same URL `alembic upgrade head` uses).",
            file=sys.stderr,
        )
        return 2

    provision(
        database_url,
        app_password=os.environ.get("CRAWMATIC_APP_DB_PASSWORD") or None,
        auth_password=os.environ.get("CRAWMATIC_AUTH_DB_PASSWORD") or None,
    )

    problems = verify(database_url)
    if problems:
        print("role posture FAILED verification:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"role posture OK: {_APP_ROLE} (no BYPASSRLS, owns nothing), "
        f"{_AUTH_ROLE} (BYPASSRLS); every workspace_id-carrying relation in public "
        "(partitions included) has RLS enabled, FORCEd and policied"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
