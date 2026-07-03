"""Live cross-workspace + global-scope scrape-profile isolation test
(SPEC-06 US1 T028, FR-021, SC-007) — ⏸ DEFERRED.

Mirrors `tests/integration/test_workspace_isolation_live.py` (SPEC-04)
and `tests/integration/test_competitors_matches_isolation_live.py`
(SPEC-05), substituting the dual-scope `scrape_profiles` surface: this
is the project's first table with a **readable-by-all, writable-by-none**
global scope (`workspace_id IS NULL`), so in addition to the standard
own/other-workspace isolation this test proves the global-read /
global-write-block halves of `emit_global_readable_rls_policy`.

Proves, on `scrape_profiles`:

1. Workspace-A's caller (a `scrape_profiles:*`-scoped API key) sees
   **0** of workspace-B's profiles — by id (`GET .../{id}` -> `404`)
   and in a list (`GET /v1/scrape-profiles` never includes them)
   (FR-004/FR-013).
2. A global (`workspace_id IS NULL`) profile, seeded out-of-band
   (research D11), is readable by **both** workspace A and workspace B
   — by id and in each workspace's list (FR-013/FR-021 read side).
3. The tenant write path cannot edit/delete the global profile: `PATCH`/
   `DELETE /v1/scrape-profiles/{global_id}` via workspace A's API key
   -> `404` (`owned_profile_get` excludes global rows, FR-021).
4. A raw, deliberately app-**unscoped** `UPDATE` with `app.workspace_id`
   set to workspace A against the global row's id affects **0** rows —
   the DB-level `FOR ALL ... WITH CHECK (workspace_id = ctx)` write
   policy blocks it even if the app-layer check were absent (defense in
   depth, FR-021).
5. With **no** `app.workspace_id` context set at all, a raw `SELECT id
   FROM scrape_profiles` returns **0** of either tenant's own rows, but
   the global row is still visible (`workspace_id IS NULL` disjunct,
   fail-closed on own rows only, FR-021/SC-007).

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced)
with the SPEC-06 migration already applied. Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever `DATABASE_URL` is unset/unreachable or the
`scrape_profiles` table doesn't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-06 migration applied).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _live_scrape_profiles_reachable() -> bool:
    url = _database_url()
    if not url:
        return False
    try:
        engine = create_engine(url)
        with engine.connect():
            pass
        from sqlalchemy import inspect

        table_names = set(inspect(engine).get_table_names())
        engine.dispose()
        if "scrape_profiles" not in table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _live_scrape_profiles_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-06 scrape_profiles "
        "migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def isolation_fixture() -> Iterator[dict[str, object]]:
    """Seed two workspaces, one own profile each, a global profile
    (`workspace_id IS NULL`, out-of-band per research D11), and a
    full-scoped API key per workspace, cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.scrape_profiles import ScrapeProfile
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        ws_a = Workspace(
            name=f"Profile Isolation A {unique}",
            slug=f"profile-isolation-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Profile Isolation B {unique}",
            slug=f"profile-isolation-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        profile_a = ScrapeProfile(workspace_id=ws_a.id, name=f"own-a-{unique}")
        profile_b = ScrapeProfile(workspace_id=ws_b.id, name=f"own-b-{unique}")
        # Out-of-band global default (research D11) — the tenant API can
        # never produce this row; seeded directly here to prove the
        # global-read half of the isolation contract.
        global_profile = ScrapeProfile(workspace_id=None, name=f"global-{unique}")
        session.add_all([profile_a, profile_b, global_profile])
        session.flush()

        secret_a, prefix_a, hash_a = generate_api_key()
        key_a = ApiKey(
            workspace_id=ws_a.id,
            name="profile-isolation-key-a",
            key_prefix=prefix_a,
            key_hash=hash_a,
            scopes=["scrape_profiles:read", "scrape_profiles:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        secret_b, prefix_b, hash_b = generate_api_key()
        key_b = ApiKey(
            workspace_id=ws_b.id,
            name="profile-isolation-key-b",
            key_prefix=prefix_b,
            key_hash=hash_b,
            scopes=["scrape_profiles:read", "scrape_profiles:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add_all([key_a, key_b])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "profile_a_id": profile_a.id,
            "profile_b_id": profile_b.id,
            "global_profile_id": global_profile.id,
            "api_key_a": secret_a,
            "api_key_b": secret_b,
        }

    try:
        yield ids
    finally:
        with get_session() as session:
            for profile_id in (
                ids["profile_a_id"],
                ids["profile_b_id"],
                ids["global_profile_id"],
            ):
                session.execute(
                    text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": profile_id}
                )
            for ws in (ids["workspace_a_id"], ids["workspace_b_id"]):
                session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": ws})
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


# --- SC-007: workspace-A caller sees 0 of workspace-B's profiles -----------


def test_workspace_a_caller_gets_404_for_workspace_b_profile_by_id(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_a']}"}
    resp = client.get(f"/v1/scrape-profiles/{isolation_fixture['profile_b_id']}", headers=headers)
    assert resp.status_code == 404


def test_workspace_a_caller_list_never_includes_workspace_b_profile(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_a']}"}
    resp = client.get("/v1/scrape-profiles", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(isolation_fixture["profile_a_id"]) in ids
    assert str(isolation_fixture["profile_b_id"]) not in ids


# --- FR-021 read side: a global profile is visible to every workspace ------


def test_global_profile_is_readable_by_workspace_a_by_id(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_a']}"}
    resp = client.get(
        f"/v1/scrape-profiles/{isolation_fixture['global_profile_id']}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] is None


def test_global_profile_is_readable_by_workspace_b_by_id(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_b']}"}
    resp = client.get(
        f"/v1/scrape-profiles/{isolation_fixture['global_profile_id']}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] is None


def test_global_profile_appears_in_both_workspaces_lists(
    client, isolation_fixture: dict[str, object]
) -> None:
    for key in ("api_key_a", "api_key_b"):
        headers = {"Authorization": f"Bearer {isolation_fixture[key]}"}
        resp = client.get("/v1/scrape-profiles", headers=headers)
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert str(isolation_fixture["global_profile_id"]) in ids


# --- FR-021 write side: tenant path cannot edit/delete a global row --------


def test_tenant_path_cannot_patch_global_profile(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_a']}"}
    resp = client.patch(
        f"/v1/scrape-profiles/{isolation_fixture['global_profile_id']}",
        headers=headers,
        json={"price_selector": ".hijacked"},
    )
    assert resp.status_code == 404


def test_tenant_path_cannot_delete_global_profile(
    client, isolation_fixture: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {isolation_fixture['api_key_a']}"}
    resp = client.delete(
        f"/v1/scrape-profiles/{isolation_fixture['global_profile_id']}", headers=headers
    )
    assert resp.status_code == 404


# --- DB-level defense in depth: RLS write policy blocks a global write -----


def test_raw_unscoped_update_of_global_profile_affects_zero_rows_via_rls(
    isolation_fixture: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(isolation_fixture["workspace_a_id"])},
        )
        result = conn.execute(
            text("UPDATE scrape_profiles SET name = name WHERE id = :id"),
            {"id": isolation_fixture["global_profile_id"]},
        )
        # FOR ALL ... WITH CHECK (workspace_id = ctx) means an UPDATE
        # targeting a NULL-workspace row under a non-NULL ctx matches (and
        # thus updates) zero rows -- the write policy blocks it even
        # though this query carries no app-layer WHERE workspace_id = ...
        # predicate at all.
        assert result.rowcount == 0


# --- fail-closed on own rows, global still visible with no context ---------


def test_no_workspace_context_hides_own_rows_but_not_global(
    isolation_fixture: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        # Deliberately no set_config('app.workspace_id', ...) call at all.
        rows = conn.execute(text("SELECT id FROM scrape_profiles")).fetchall()

    ids = {row[0] for row in rows}
    assert isolation_fixture["profile_a_id"] not in ids
    assert isolation_fixture["profile_b_id"] not in ids
    assert isolation_fixture["global_profile_id"] in ids
