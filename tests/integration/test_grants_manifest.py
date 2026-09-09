"""Integration test for the component-scoped grants manifest (EPA A3/F03).

    docker compose up -d postgres redis
    TENANT_ISOLATION_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:5432/crawmatic \\
        uv run pytest tests/integration/test_grants_manifest.py -v -m integration

Provisions a database from zero exactly the way a real deploy does —
``alembic upgrade head`` (creating ``workspace_usage_v`` along the way),
then ``scripts/provision_db_roles.py``'s ``provision()`` (which executes
``scripts/provision_db_roles.sql``, including its new component-scoped
grant loops and the ``crawmatic_scraper`` role) — then runs the three
checks the packet's acceptance criteria name against that one database:

1. ``scripts/verify_grants.py``'s live-grant diff against
   ``scripts/sql/grants_expected.yaml`` reports no drift.
2. The existing partition-RLS guard, ``scripts/rls_verify.py``, still
   passes — this task narrows privileges but must not touch the RLS
   posture ``tests/integration/test_tenant_isolation_roles.py`` and this
   script already prove.
3. The new ``crawmatic_scraper`` role exists with exactly its named
   narrow privileges (spot-checked directly, not only via the generic
   diff, so a bug in the manifest itself would not hide a bug in the
   role's actual grants).

Reuses the exact fixture pattern (env var names, throwaway passwords,
`_admin_url`/`_reachable`/`_with_role` helpers) from
``tests/integration/test_tenant_isolation_roles.py`` rather than
importing across test modules, matching that file's own stated reason:
collection must work (reporting a skip, not an import error) even when
no Postgres is reachable at all.
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
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from provision_db_roles import (  # noqa: E402
    APP_ROLE,
    MIGRATE_ROLE,
    SCRAPER_ROLE,
    SYSTEM_ROLE,
    provision,
    verify,
)
from verify_grants import load_manifest, run as run_verify_grants  # noqa: E402

pytestmark = pytest.mark.integration

_APP_PW = "a3-grants-app-pw"  # noqa: S105 - throwaway test database only
_AUTH_PW = "a3-grants-auth-pw"  # noqa: S105 - throwaway test database only
_SCRAPER_PW = "a3-grants-scraper-pw"  # noqa: S105 - throwaway test database only
_MIGRATE_PW = "a3-grants-migrate-pw"  # noqa: S105 - throwaway test database only

_HOWTO = """
No reachable owner/admin database URL for the grants-manifest suite.

Set TENANT_ISOLATION_TEST_DATABASE_URL (or RLS_TEST_DATABASE_URL /
MIGRATION_DATABASE_URL) to an owner-role URL for a THROWAWAY Postgres,
e.g. the compose stack:

  docker compose up -d postgres redis
  TENANT_ISOLATION_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:5432/crawmatic \\
    uv run pytest tests/integration/test_grants_manifest.py -v -m integration

The suite migrates the database and provisions all four roles itself.
"""


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


@pytest.fixture(scope="module")
def provisioned() -> Iterator[dict[str, str]]:
    """A migrated database with all four roles' component-scoped grants applied."""
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
        scraper_password=_SCRAPER_PW,
        migrate_password=_MIGRATE_PW,
    )

    # The posture is an ASSERTION, not an assumption, exactly like the
    # tenant-isolation suite: a misprovisioned role would make every
    # grants claim below vacuous.
    failures = [f for f in verify(admin_url) if f.severity == "FAIL"]
    assert failures == [], "role posture is wrong before any grants claim:\n" + "\n".join(
        f.render() for f in failures
    )

    # rls_verify.py's positive control (own context sees own rows) needs
    # at least one real, populated workspace to compare against -- a
    # freshly migrated database has none. One workspace + one product
    # row, seeded through the admin/owner connection (never through a
    # runtime role -- the point is confining data an unprivileged role
    # must not otherwise see).
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            existing = conn.execute(text("SELECT count(*) FROM workspaces")).scalar_one()
            if existing == 0:
                ws_id = str(uuid.uuid4())
                conn.execute(
                    text(
                        "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                        "VALUES (:id, 'A3 grants suite', :slug, 'ACTIVE', now(), now())"
                    ),
                    {"id": ws_id, "slug": f"a3-grants-{ws_id[:8]}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO products (id, workspace_id, title, status, created_at, updated_at) "
                        "VALUES (:id, :ws, 'A3 grants suite probe product', 'ACTIVE', now(), now())"
                    ),
                    {"id": str(uuid.uuid4()), "ws": ws_id},
                )
    finally:
        engine.dispose()

    yield {
        "admin_url": admin_url,
        "app_url": _with_role(admin_url, APP_ROLE, _APP_PW),
        "system_url": _with_role(admin_url, SYSTEM_ROLE, _AUTH_PW),
        "scraper_url": _with_role(admin_url, SCRAPER_ROLE, _SCRAPER_PW),
    }


# =====================================================================
# 1. verify_grants.py reports no drift against the live database
# =====================================================================


def test_verify_grants_reports_no_drift(
    provisioned: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_verify_grants(provisioned["admin_url"])
    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"verify_grants.py found drift after provisioning from the manifest:\n"
        f"{captured.out}\n{captured.err}"
    )


# =====================================================================
# 2. The existing partition-RLS guard still passes
# =====================================================================


def test_rls_verify_still_passes(
    provisioned: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """scripts/rls_verify.py, unmodified by this task, must still pass against
    the crawmatic_app role after its privileges were narrowed."""
    import rls_verify

    monkeypatch.setenv("RLS_VERIFY_DATABASE_URL", provisioned["app_url"])
    exit_code = rls_verify.main(["--txn-probe"])
    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"rls_verify.py failed after A3's grant narrowing:\n{captured.out}\n{captured.err}"
    )


# =====================================================================
# 3. crawmatic_scraper holds exactly its named narrow privileges
# =====================================================================


def test_scraper_role_exists_with_named_attributes(provisioned: dict[str, str]) -> None:
    engine = create_engine(provisioned["admin_url"])
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                    "WHERE rolname = :role"
                ),
                {"role": SCRAPER_ROLE},
            ).one_or_none()
    finally:
        engine.dispose()
    assert row is not None, f"{SCRAPER_ROLE} was not created by provisioning"
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    assert row.rolcanlogin is True


def test_scraper_role_has_named_privileges_and_nothing_else(
    provisioned: dict[str, str],
) -> None:
    """Spot-check the scraper's grants directly against pg_catalog — not just
    via verify_grants.py's own diff, so a bug in the manifest itself would not
    hide a bug in the role's actual grants."""
    manifest = load_manifest()
    scraper = manifest["crawmatic_scraper"]
    expected_nonempty = {t: privs for t, privs in scraper.items() if privs}
    assert expected_nonempty == {
        "request_attempts": {"INSERT"},
        "price_observations": {"INSERT"},
        "network_operations": {"INSERT"},
        "scrape_job_targets": {"UPDATE"},
        "domain_strategy_profiles": {"SELECT"},
        "scrape_profiles": {"SELECT"},
        "competitor_product_matches": {"SELECT"},
    }

    engine = create_engine(provisioned["admin_url"])
    try:
        with engine.connect() as conn:
            for table, privileges in ("cost_budgets", set()), ("workspace_entitlements", set()), ("users", set()):
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    has = conn.execute(
                        text("SELECT has_table_privilege(:role, :table, :priv)"),
                        {"role": SCRAPER_ROLE, "table": table, "priv": priv},
                    ).scalar_one()
                    assert not has, (
                        f"{SCRAPER_ROLE} unexpectedly holds {priv} on {table} "
                        "(acceptance criterion: nothing on budgets/entitlements/users)"
                    )
            for table, privs in expected_nonempty.items():
                for priv in privs:
                    has = conn.execute(
                        text("SELECT has_table_privilege(:role, :table, :priv)"),
                        {"role": SCRAPER_ROLE, "table": table, "priv": priv},
                    ).scalar_one()
                    assert has, f"{SCRAPER_ROLE} is missing {priv} on {table}"
    finally:
        engine.dispose()


# =====================================================================
# workspace_usage_v exists and is readable by crawmatic_app
# =====================================================================


def test_workspace_usage_view_exists_and_is_readable_by_app(
    provisioned: dict[str, str],
) -> None:
    engine = create_engine(provisioned["admin_url"])
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass('public.workspace_usage_v') IS NOT NULL")
            ).scalar()
            assert exists, "workspace_usage_v was not created by the A3 migration"
            has_select = conn.execute(
                text("SELECT has_table_privilege(:role, 'workspace_usage_v', 'SELECT')"),
                {"role": APP_ROLE},
            ).scalar_one()
            assert has_select, f"{APP_ROLE} cannot SELECT workspace_usage_v"
    finally:
        engine.dispose()

    # And a connection actually authenticated as crawmatic_app can query
    # it (empty result is fine -- no ledger rows exist in this fresh
    # database; the point is that the grant + view definition are both
    # valid SQL a real connection can execute).
    app_engine = create_engine(provisioned["app_url"], connect_args={"prepare_threshold": None})
    try:
        with app_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.workspace_id', '', true)"))
            conn.execute(text("SELECT * FROM workspace_usage_v LIMIT 1"))
    finally:
        app_engine.dispose()
