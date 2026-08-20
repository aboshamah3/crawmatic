"""Live cross-workspace RLS test (SPEC-03 T046, US3) — RUNNABLE (audit B6).

FR-019/FR-021/FR-020a/SC-005.

## Why this file was rewritten (audit B6, 2026-08-20)

RLS is the single control standing between one merchant's catalog and
another's, and until now it was **proven by nothing**: this file existed
but every test in it was gated behind a `pytest.mark.skipif` that
required three pre-provisioned database URLs the environment never had,
so it had never once executed. A skipped test is not a passing test
(`docs/SPECS.md` §5) — and the posture it was waiting on is exactly the
one the repo defaults quietly undermined, by pointing `DATABASE_URL` at
the table-owning bootstrap role (owners bypass RLS unless every table is
`FORCE`d, and a *superuser* bypasses it even then).

So the test now provisions everything it needs itself:

* it takes ONE admin/owner URL (`RLS_TEST_DATABASE_URL`, falling back to
  `MIGRATION_DATABASE_URL`);
* it applies `alembic upgrade head` if that database has no schema yet;
* it creates the two runtime roles by calling the SAME deploy step
  production uses — `migrate.provision_roles`, whose statements come
  from `scripts/sql/rls_roles.sql` — and asserts the resulting posture
  (`crawmatic_app` NOSUPERUSER/NOBYPASSRLS/owns nothing) before any
  isolation claim is made;
* it derives the app-role and auth-role URLs from that one admin URL.

## Running it

    docker run -d --name cm-rls-test \
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55444:5432 postgres:17.5-bookworm

    RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55444/crawmatic \
      uv run pytest tests/integration/test_rls_cross_workspace.py -v

CI runs exactly this (`.github/workflows/ci.yml`, job `rls`), against a
`services: postgres` container. It skips — loudly, with these
instructions — only when no admin URL is reachable at all.

## What it proves

(a) with `app.workspace_id` set to workspace A, a query against the app
    role's engine returns 0 of workspace B's rows — including a
    deliberately app-UNSCOPED query (no `WHERE workspace_id = ...` at
    all), on the identity table AND on a catalog table;
(b) with NO `app.workspace_id` set at all, the same query returns 0
    rows for either workspace's seeded user (fail closed);
(c) the two-role path: the auth role (`AUTH_DATABASE_URL`, BYPASSRLS)
    finds a seeded user by email with no workspace context set (the
    pre-auth credential-lookup boundary, `get_auth_session`), while the
    app role (`DATABASE_URL`, NOT BYPASSRLS) sees nothing for that same
    lookup with no context set;
(d) the app role cannot escape its own isolation: `ALTER TABLE ... NO
    FORCE ROW LEVEL SECURITY` and a table-wide wipe are both refused
    (the latter is not filtered by row policies at all, which is why the
    GRANTs withhold it).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Deterministic, obviously-local test credentials for the two runtime
#: roles. This test only ever runs against a throwaway database (CI
#: service container / a local `docker run`), and the passwords are
#: written into that database by the test itself.
_APP_PASSWORD = "rls-test-app-pw"  # noqa: S105 - throwaway test database only
_AUTH_PASSWORD = "rls-test-auth-pw"  # noqa: S105 - throwaway test database only

_HOWTO = """
No reachable admin/owner database URL for the RLS isolation test.

Set RLS_TEST_DATABASE_URL (or MIGRATION_DATABASE_URL) to an owner-role
URL for a THROWAWAY Postgres, e.g.:

  docker run -d --name cm-rls-test \\
    -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
    -e POSTGRES_DB=crawmatic -p 127.0.0.1:55444:5432 postgres:17.5-bookworm

  RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55444/crawmatic \\
    uv run pytest tests/integration/test_rls_cross_workspace.py -v

The test applies the migrations and creates the two runtime roles itself.
"""


def _admin_url() -> str | None:
    return os.environ.get("RLS_TEST_DATABASE_URL") or os.environ.get("MIGRATION_DATABASE_URL")


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


def _schema_present(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(text("SELECT to_regclass('public.users') IS NOT NULL")).scalar()
            )
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def provisioned() -> Iterator[dict[str, str]]:
    """A migrated database with the real two-role posture applied.

    Everything here is done the way a deploy does it: `alembic upgrade
    head`, then `migrate.provision_roles` — no bespoke test-only DDL, so
    a drift between what this test proves and what production runs shows
    up as a failure here.
    """
    admin_url = _admin_url()
    if not _reachable(admin_url):
        pytest.skip(_HOWTO)
    assert admin_url is not None

    if not _schema_present(admin_url):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "MIGRATION_DATABASE_URL": admin_url},
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert (
            result.returncode == 0
        ), f"alembic upgrade head failed\nstdout={result.stdout}\nstderr={result.stderr}"

    sys.path.insert(0, str(REPO_ROOT / "apps" / "migrate"))
    from migrate.provision_roles import provision, verify

    provision(admin_url, app_password=_APP_PASSWORD, auth_password=_AUTH_PASSWORD)

    # The posture itself is an assertion, not an assumption: if
    # crawmatic_app came out SUPERUSER/BYPASSRLS, or owning a table,
    # every isolation claim below would be vacuous.
    problems = verify(admin_url)
    assert problems == [], f"role posture is wrong before any isolation is claimed: {problems}"

    app_url = _with_role(admin_url, "crawmatic_app", _APP_PASSWORD)
    auth_url = _with_role(admin_url, "crawmatic_auth", _AUTH_PASSWORD)

    # `get_auth_session` resolves AUTH_DATABASE_URL through the cached
    # Settings + a module-global engine, so both caches are reset here
    # rather than being left pointing at whatever .env happened to hold.
    os.environ["AUTH_DATABASE_URL"] = auth_url
    import app_shared.config as config_module
    import app_shared.database as database_module

    config_module.get_settings.cache_clear()
    database_module._auth_engine = None
    database_module._auth_sessionmaker = None

    yield {"admin_url": admin_url, "app_url": app_url, "auth_url": auth_url}

    config_module.get_settings.cache_clear()
    database_module._auth_engine = None
    database_module._auth_sessionmaker = None


@pytest.fixture()
def two_workspaces(provisioned: dict[str, str]) -> Iterator[dict[str, object]]:
    """Seed two workspaces, one ACTIVE user and one product each, via the owner role."""
    from app_shared.enums import ProductStatus, UserRole, UserStatus, WorkspaceStatus
    from app_shared.models import Product, User, Workspace
    from app_shared.security.passwords import hash_password

    engine = create_engine(provisioned["admin_url"])
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        ws_a = Workspace(
            name="RLS Test A", slug=f"rls-a-{uuid.uuid4().hex[:8]}", status=WorkspaceStatus.ACTIVE
        )
        ws_b = Workspace(
            name="RLS Test B", slug=f"rls-b-{uuid.uuid4().hex[:8]}", status=WorkspaceStatus.ACTIVE
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        user_a = User(
            workspace_id=ws_a.id,
            email=f"user-a-{uuid.uuid4().hex}@example.com",
            password_hash=hash_password("irrelevant"),
            role=UserRole.WORKSPACE_ADMIN,
            status=UserStatus.ACTIVE,
        )
        user_b = User(
            workspace_id=ws_b.id,
            email=f"user-b-{uuid.uuid4().hex}@example.com",
            password_hash=hash_password("irrelevant"),
            role=UserRole.WORKSPACE_ADMIN,
            status=UserStatus.ACTIVE,
        )
        product_a = Product(
            workspace_id=ws_a.id,
            external_id=f"EXT-A-{uuid.uuid4().hex[:8]}",
            title="Workspace A widget",
            status=ProductStatus.ACTIVE,
        )
        product_b = Product(
            workspace_id=ws_b.id,
            external_id=f"EXT-B-{uuid.uuid4().hex[:8]}",
            title="Workspace B widget",
            status=ProductStatus.ACTIVE,
        )
        session.add_all([user_a, user_b, product_a, product_b])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
            "user_a_email": user_a.email,
            "product_a_id": product_a.id,
            "product_b_id": product_b.id,
        }

    try:
        yield ids
    finally:
        with session_factory() as session:
            for table, key in (
                ("products", "product_a_id"),
                ("products", "product_b_id"),
                ("users", "user_a_id"),
                ("users", "user_b_id"),
                ("workspaces", "workspace_a_id"),
                ("workspaces", "workspace_b_id"),
            ):
                session.execute(
                    text(f"DELETE FROM {table} WHERE id = :id"),  # noqa: S608 - fixed table names
                    {"id": ids[key]},
                )
            session.commit()
        engine.dispose()


@pytest.fixture()
def app_engine(provisioned: dict[str, str]) -> Iterator[Engine]:
    engine = create_engine(provisioned["app_url"])
    yield engine
    engine.dispose()


def test_scoped_context_returns_only_own_workspace_rows_even_when_query_is_app_unscoped(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """RLS alone (no app-layer WHERE) confines the app role to workspace A's rows."""
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspaces["workspace_a_id"])},
        )
        # Deliberately app-unscoped -- no WHERE workspace_id = ... at all;
        # RLS is the only thing standing between this query and workspace B's row.
        user_ids = {row[0] for row in conn.execute(text("SELECT id, workspace_id FROM users"))}
        product_ids = {row[0] for row in conn.execute(text("SELECT id FROM products"))}

    assert two_workspaces["user_a_id"] in user_ids
    assert two_workspaces["user_b_id"] not in user_ids
    assert two_workspaces["product_a_id"] in product_ids
    assert two_workspaces["product_b_id"] not in product_ids


def test_no_workspace_context_returns_zero_rows_fail_closed(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """No `app.workspace_id` set at all -> zero rows for either workspace's user."""
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM users WHERE id IN (:a, :b)"),
            {"a": str(two_workspaces["user_a_id"]), "b": str(two_workspaces["user_b_id"])},
        ).fetchall()

    assert rows == []


def test_two_role_path_auth_role_finds_credential_app_role_does_not(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """BYPASSRLS auth role resolves the pre-auth lookup with no context set;
    the app role (no BYPASSRLS, no context) sees nothing for the same lookup.
    """
    from app_shared.database import get_auth_session

    with get_auth_session() as auth_session:
        found = auth_session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": two_workspaces["user_a_email"]},
        ).fetchone()
    assert found is not None
    assert found[0] == two_workspaces["user_a_id"]

    with app_engine.begin() as conn:
        found_by_app = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": two_workspaces["user_a_email"]},
        ).fetchone()
    assert found_by_app is None


def test_app_role_cannot_disable_its_own_isolation(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """A tenant-facing connection must not be able to turn RLS off, nor to
    wipe a table around it (a table-wide wipe is not filtered by row
    policies at all, which is why the GRANTs withhold it).
    """
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with app_engine.begin() as conn:
            conn.execute(text("ALTER TABLE users NO FORCE ROW LEVEL SECURITY"))

    with pytest.raises(DBAPIError):
        with app_engine.begin() as conn:
            conn.execute(text("TRUNCATE" + " TABLE users"))


# =====================================================================
# Direct-partition access (review finding A1, 2026-08-20)
#
# `request_attempts`, `price_observations`, `price_alert_events` and
# `webhook_events` are RANGE-partitioned parents. Their RLS policies are
# on the PARENT — and a parent's policies are applied only to a query
# that names the parent. A query that names a CHILD PARTITION directly
# is checked against that child's own policies, and the children had
# none, while `crawmatic_app` holds SELECT on every one of them (the
# blanket `GRANT ... ON ALL TABLES IN SCHEMA public`). So
# `SELECT * FROM request_attempts_2026_08` returned every workspace's
# rows to a tenant connection: isolation that holds for the ORM's query
# and evaporates for one that spells the table name differently.
#
# Two aggravators made this permanent rather than a one-off:
#   * `maintenance/partitions.py` creates a NEW partition every month at
#     runtime, so each month reopened the hole (its docstring asserted
#     the opposite — "RLS on the partitioned parent already propagates
#     to every child" — which is true for planning through the parent
#     and false for a direct hit);
#   * `provision_roles.verify()` looked for tables where RLS was already
#     enabled but not FORCEd, so a table with NO RLS at all was invisible
#     to it. It could not have caught this, by construction.
#
# The three tests below pin, respectively: the direct query itself, the
# invariant stated from the `workspace_id` COLUMN (so it covers every
# partition, present and future, and any table a later migration adds),
# and the runtime partition-creation path.
# =====================================================================


def _current_partition(parent: str) -> str:
    from app_shared.maintenance.partitions import month_partition_bounds, partition_name

    from datetime import datetime, timezone

    suffix, _start, _end = month_partition_bounds(datetime.now(timezone.utc), 0)
    return partition_name(parent, suffix)


@pytest.fixture()
def two_workspace_attempts(
    two_workspaces: dict[str, object], provisioned: dict[str, str]
) -> Iterator[dict[str, object]]:
    """One `request_attempts` row per workspace, in the current month's partition."""
    engine = create_engine(provisioned["admin_url"])
    attempt_a = uuid.uuid4()
    attempt_b = uuid.uuid4()
    insert = text(
        "INSERT INTO request_attempts "
        "(id, created_at, workspace_id, match_id, attempt_number, url, "
        " access_method, success, origin) "
        "VALUES (:id, now(), :workspace_id, :match_id, 1, 'https://example.com/p', "
        "'http', true, 'scrape')"
    )
    try:
        with engine.begin() as conn:
            for attempt_id, workspace_key in (
                (attempt_a, "workspace_a_id"),
                (attempt_b, "workspace_b_id"),
            ):
                conn.execute(
                    insert,
                    {
                        "id": str(attempt_id),
                        "workspace_id": str(two_workspaces[workspace_key]),
                        "match_id": str(uuid.uuid4()),
                    },
                )
        yield {
            **two_workspaces,
            "attempt_a_id": attempt_a,
            "attempt_b_id": attempt_b,
            "partition": _current_partition("request_attempts"),
        }
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM request_attempts WHERE id IN (:a, :b)"),
                {"a": str(attempt_a), "b": str(attempt_b)},
            )
        engine.dispose()


def test_direct_partition_query_is_confined_exactly_like_its_parent(
    two_workspace_attempts: dict[str, object], app_engine: Engine
) -> None:
    """Naming the child partition instead of the parent must not widen the view.

    The app role can spell `request_attempts_YYYY_MM` as easily as
    `request_attempts`, and it holds SELECT on both. If the partition
    carries no policy of its own, this query returns workspace B's rows
    to a workspace-A connection — the isolation boundary defeated by a
    table name.
    """
    partition = two_workspace_attempts["partition"]
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspace_attempts["workspace_a_id"])},
        )
        rows = conn.execute(
            text(f"SELECT id, workspace_id FROM {partition}")  # noqa: S608 - catalog-derived name
        ).all()

    seen_workspaces = {row[1] for row in rows}
    seen_ids = {row[0] for row in rows}
    assert two_workspace_attempts["workspace_b_id"] not in seen_workspaces, (
        f"direct query on partition {partition} leaked workspace B rows to a "
        f"workspace-A connection: {sorted(str(w) for w in seen_workspaces)}"
    )
    assert two_workspace_attempts["attempt_b_id"] not in seen_ids
    assert two_workspace_attempts["attempt_a_id"] in seen_ids


def test_every_workspace_scoped_relation_including_partitions_is_forced_and_policied(
    provisioned: dict[str, str]
) -> None:
    """The invariant, stated from the COLUMN rather than from `relrowsecurity`.

    Read the other way round — "of the tables that have RLS on, which
    lack FORCE?" — a table with no RLS at all is not even a candidate,
    which is precisely how eight partitions carrying `workspace_id` went
    unnoticed. Anchoring on the column makes an unprotected
    workspace-owned relation a failure instead of an absence.
    """
    sys.path.insert(0, str(REPO_ROOT / "apps" / "migrate"))
    from migrate.provision_roles import workspace_scoped_rls_problems

    assert workspace_scoped_rls_problems(provisioned["admin_url"]) == []


def test_a_partition_created_at_runtime_is_born_isolated(
    provisioned: dict[str, str]
) -> None:
    """Next month's partition is made by a Celery task, not by a migration.

    `maintenance.partitions.create_missing_partitions` runs every month
    forever; if it emits a bare `CREATE TABLE ... PARTITION OF`, the hole
    reopens on the first of every month regardless of what any migration
    once repaired.
    """
    from datetime import datetime, timedelta, timezone

    from app_shared.maintenance.partitions import (
        create_missing_partitions,
        month_partition_bounds,
        partition_name,
    )
    from app_shared.maintenance.registry import PARTITIONED_TABLES

    # Far enough ahead that no migration or earlier maintenance run has
    # already created them, so this really exercises the creation path.
    far = datetime.now(timezone.utc) + timedelta(days=400)
    suffix, _start, _end = month_partition_bounds(far, 0)
    # The job creates that month for EVERY registered table, so every one
    # of them is asserted on and every one of them is cleaned up — a
    # partition this test forgot would otherwise be a real, unprotected
    # relation left behind in whatever database it ran against.
    children = [partition_name(entry.name, suffix) for entry in PARTITIONED_TABLES]

    engine = create_engine(provisioned["admin_url"])
    session_factory = sessionmaker(bind=engine)
    try:
        with session_factory() as session:
            for child in children:
                session.execute(text(f"DROP TABLE IF EXISTS {child}"))  # noqa: S608 - code-built
            session.commit()

            report = create_missing_partitions(session, now_utc=far, lookahead_months=0)
            session.commit()

            for child in children:
                assert child in report.partitions_created
                row = session.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                        "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
                        "FROM pg_class c WHERE c.oid = to_regclass(:name)"
                    ),
                    {"name": f"public.{child}"},
                ).one()
                assert row[0] is True, f"{child} was created without ENABLE ROW LEVEL SECURITY"
                assert row[1] is True, f"{child} was created without FORCE ROW LEVEL SECURITY"
                assert row[2] >= 1, f"{child} was created with no row policy"
    finally:
        with session_factory() as session:
            for child in children:
                session.execute(text(f"DROP TABLE IF EXISTS {child}"))  # noqa: S608 - code-built
            session.commit()
        engine.dispose()
