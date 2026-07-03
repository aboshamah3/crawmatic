"""Live-Postgres profile-assignment test (SPEC-06 US2 T035, FR-012/FR-013/FR-023,
SC-002) — ⏸ DEFERRED.

Exercises assignment enforcement across the three surfaces that accept a
`scrape_profile_id`/`default_scrape_profile_id` (`contracts/assignment-
enforcement.md`) against a real database through FastAPI's `TestClient`
(no running server/container required — only the database needs to be
live):

1. Assigning a profile as a competitor default (`POST`/`PATCH
   /v1/competitors`), a match override (`POST`/`PATCH /v1/matches`), and
   the workspace default (`PUT /v1/scrape-profiles/workspace-default`)
   is accepted for the caller's own profile and for a global
   (`workspace_id IS NULL`) profile.
2. The same three surfaces reject a cross-workspace profile id with a
   clean `422 WORKSPACE_MISMATCH` (`assert_profile_assignable`,
   FR-013), never storing the reference.
3. `null` clears an existing assignment on all three surfaces.
4. Deleting a profile that is referenced by a competitor default, a
   match override, and the workspace default nulls **every** reference
   (`ON DELETE SET NULL`, FR-023) — proven via the `/v1/competitors`
   and `/v1/matches` read paths (`default_scrape_profile_id`/
   `scrape_profile_id`) and a direct read of `workspaces.
   default_scrape_profile_id`.

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-06 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `scrape_profiles` table doesn't exist yet (mirrors
`tests/integration/test_scrape_profiles_bulk_upsert_live.py`'s skip
mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-06 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest


def _live_profile_assignment_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-06 `scrape_profiles` table (+ the three FK promotions it drives)
    already exists (migration applied)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        inspector = inspect(get_engine())
        table_names = set(inspector.get_table_names())
        if not {"scrape_profiles", "competitors", "competitor_product_matches"} <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_profile_assignment_reachable(),
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
def assignment_fixture() -> Iterator[dict[str, object]]:
    """Two workspaces (A, B), one own profile per workspace, one global
    profile (seeded out-of-band per research D11), one product+variant in
    workspace A, and a full-scoped API key per workspace, cleaned up
    after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.models.scrape_profiles import ScrapeProfile
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]
    scopes = [
        "competitors:read",
        "competitors:write",
        "matches:read",
        "matches:write",
        "scrape_profiles:read",
        "scrape_profiles:write",
    ]

    with get_session() as session:
        ws_a = Workspace(
            name=f"Assignment Live A {unique}",
            slug=f"assignment-live-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Assignment Live B {unique}",
            slug=f"assignment-live-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        profile_a = ScrapeProfile(workspace_id=ws_a.id, name=f"own-a-{unique}")
        profile_b = ScrapeProfile(workspace_id=ws_b.id, name=f"own-b-{unique}")
        # Out-of-band global default (research D11) — the tenant API can
        # never produce this row; seeded directly here.
        global_profile = ScrapeProfile(workspace_id=None, name=f"global-{unique}")
        session.add_all([profile_a, profile_b, global_profile])
        session.flush()

        product = Product(
            workspace_id=ws_a.id,
            title="Assignment Live Test Product",
            status=ProductStatus.ACTIVE,
        )
        session.add(product)
        session.flush()

        variant = ProductVariant(
            workspace_id=ws_a.id,
            product_id=product.id,
            title="Default",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add(variant)
        session.flush()

        secret_a, prefix_a, hash_a = generate_api_key()
        key_a = ApiKey(
            workspace_id=ws_a.id,
            name="assignment-live-key-a",
            key_prefix=prefix_a,
            key_hash=hash_a,
            scopes=scopes,
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(key_a)
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "profile_a_id": profile_a.id,
            "profile_b_id": profile_b.id,
            "global_profile_id": global_profile.id,
            "product_variant_id": variant.id,
            "api_key_a": secret_a,
        }

    try:
        yield ids
    finally:
        with get_session() as session:
            from sqlalchemy import text

            session.execute(
                text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"),
                {"ws": ids["workspace_a_id"]},
            )
            session.execute(
                text("DELETE FROM competitors WHERE workspace_id = :ws"),
                {"ws": ids["workspace_a_id"]},
            )
            session.execute(
                text("DELETE FROM product_variants WHERE workspace_id = :ws"),
                {"ws": ids["workspace_a_id"]},
            )
            session.execute(
                text("DELETE FROM products WHERE workspace_id = :ws"),
                {"ws": ids["workspace_a_id"]},
            )
            for profile_id in (
                ids["profile_a_id"],
                ids["profile_b_id"],
                ids["global_profile_id"],
            ):
                session.execute(
                    text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": profile_id}
                )
            for ws in (ids["workspace_a_id"], ids["workspace_b_id"]):
                session.execute(
                    text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


@pytest.fixture()
def auth_headers(assignment_fixture: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {assignment_fixture['api_key_a']}"}


# --- competitor default_scrape_profile_id -----------------------------------


def test_create_competitor_with_own_profile_default_is_accepted(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Own Default Competitor",
            "domain": f"own-default-{uuid.uuid4().hex[:8]}.example.com",
            "default_scrape_profile_id": str(assignment_fixture["profile_a_id"]),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["default_scrape_profile_id"] == str(assignment_fixture["profile_a_id"])


def test_create_competitor_with_global_profile_default_is_accepted(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Global Default Competitor",
            "domain": f"global-default-{uuid.uuid4().hex[:8]}.example.com",
            "default_scrape_profile_id": str(assignment_fixture["global_profile_id"]),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["default_scrape_profile_id"] == str(
        assignment_fixture["global_profile_id"]
    )


def test_create_competitor_with_cross_workspace_profile_default_is_422(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Cross-WS Default Competitor",
            "domain": f"cross-ws-{uuid.uuid4().hex[:8]}.example.com",
            "default_scrape_profile_id": str(assignment_fixture["profile_b_id"]),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "WORKSPACE_MISMATCH"


def test_update_competitor_default_profile_to_null_clears_it(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    create = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Clearable Default Competitor",
            "domain": f"clearable-{uuid.uuid4().hex[:8]}.example.com",
            "default_scrape_profile_id": str(assignment_fixture["profile_a_id"]),
        },
    )
    assert create.status_code == 201
    competitor_id = create.json()["id"]

    update = client.patch(
        f"/v1/competitors/{competitor_id}",
        headers=auth_headers,
        json={"default_scrape_profile_id": None},
    )
    assert update.status_code == 200
    assert update.json()["default_scrape_profile_id"] is None


def test_update_competitor_default_profile_to_cross_workspace_is_422(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    create = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Reassign Competitor",
            "domain": f"reassign-{uuid.uuid4().hex[:8]}.example.com",
        },
    )
    assert create.status_code == 201
    competitor_id = create.json()["id"]

    update = client.patch(
        f"/v1/competitors/{competitor_id}",
        headers=auth_headers,
        json={"default_scrape_profile_id": str(assignment_fixture["profile_b_id"])},
    )
    assert update.status_code == 422
    assert update.json()["detail"]["error"]["code"] == "WORKSPACE_MISMATCH"


# --- match scrape_profile_id -------------------------------------------------


@pytest.fixture()
def competitor_id(client, auth_headers) -> str:
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={
            "name": "Match Assignment Competitor",
            "domain": f"match-assign-{uuid.uuid4().hex[:8]}.example.com",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_match_with_own_profile_override_is_accepted(
    client, auth_headers, assignment_fixture: dict[str, object], competitor_id: str
) -> None:
    resp = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": str(assignment_fixture["product_variant_id"]),
            "competitor_id": competitor_id,
            "competitor_url": f"https://competitor.example.com/p/{uuid.uuid4().hex[:8]}",
            "scrape_profile_id": str(assignment_fixture["profile_a_id"]),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scrape_profile_id"] == str(assignment_fixture["profile_a_id"])


def test_create_match_with_cross_workspace_profile_override_is_422(
    client, auth_headers, assignment_fixture: dict[str, object], competitor_id: str
) -> None:
    resp = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": str(assignment_fixture["product_variant_id"]),
            "competitor_id": competitor_id,
            "competitor_url": f"https://competitor.example.com/p/{uuid.uuid4().hex[:8]}",
            "scrape_profile_id": str(assignment_fixture["profile_b_id"]),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "WORKSPACE_MISMATCH"


def test_update_match_profile_to_null_clears_it(
    client, auth_headers, assignment_fixture: dict[str, object], competitor_id: str
) -> None:
    create = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": str(assignment_fixture["product_variant_id"]),
            "competitor_id": competitor_id,
            "competitor_url": f"https://competitor.example.com/p/{uuid.uuid4().hex[:8]}",
            "scrape_profile_id": str(assignment_fixture["profile_a_id"]),
        },
    )
    assert create.status_code == 201
    match_id = create.json()["id"]

    update = client.patch(
        f"/v1/matches/{match_id}",
        headers=auth_headers,
        json={"scrape_profile_id": None},
    )
    assert update.status_code == 200
    assert update.json()["scrape_profile_id"] is None


# --- workspace-default assignment --------------------------------------------


def test_put_workspace_default_own_profile_is_accepted(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": str(assignment_fixture["profile_a_id"])},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile_id"] == str(assignment_fixture["profile_a_id"])


def test_put_workspace_default_global_profile_is_accepted(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": str(assignment_fixture["global_profile_id"])},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile_id"] == str(assignment_fixture["global_profile_id"])


def test_put_workspace_default_cross_workspace_profile_is_422(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    resp = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": str(assignment_fixture["profile_b_id"])},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "WORKSPACE_MISMATCH"


def test_put_workspace_default_null_clears_it(
    client, auth_headers, assignment_fixture: dict[str, object]
) -> None:
    set_resp = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": str(assignment_fixture["profile_a_id"])},
    )
    assert set_resp.status_code == 200

    clear_resp = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": None},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["profile_id"] is None


# --- FR-023: deleting a referenced profile nulls every reference ------------


def test_deleting_referenced_profile_nulls_every_reference(
    client, auth_headers, assignment_fixture: dict[str, object], competitor_id: str
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    # Create a fresh profile referenced by all three surfaces.
    create_profile = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={"name": f"deletable-{uuid.uuid4().hex[:8]}"},
    )
    assert create_profile.status_code == 201, create_profile.text
    profile_id = create_profile.json()["id"]

    patch_competitor = client.patch(
        f"/v1/competitors/{competitor_id}",
        headers=auth_headers,
        json={"default_scrape_profile_id": profile_id},
    )
    assert patch_competitor.status_code == 200

    create_match = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": str(assignment_fixture["product_variant_id"]),
            "competitor_id": competitor_id,
            "competitor_url": f"https://competitor.example.com/p/{uuid.uuid4().hex[:8]}",
            "scrape_profile_id": profile_id,
        },
    )
    assert create_match.status_code == 201, create_match.text
    match_id = create_match.json()["id"]

    set_default = client.put(
        "/v1/scrape-profiles/workspace-default",
        headers=auth_headers,
        json={"profile_id": profile_id},
    )
    assert set_default.status_code == 200

    delete = client.delete(f"/v1/scrape-profiles/{profile_id}", headers=auth_headers)
    assert delete.status_code == 200

    competitor_after = client.get(f"/v1/competitors/{competitor_id}", headers=auth_headers)
    assert competitor_after.status_code == 200
    assert competitor_after.json()["default_scrape_profile_id"] is None

    match_after = client.get(f"/v1/matches/{match_id}", headers=auth_headers)
    assert match_after.status_code == 200
    assert match_after.json()["scrape_profile_id"] is None

    with get_session() as session:
        row = session.execute(
            text("SELECT default_scrape_profile_id FROM workspaces WHERE id = :ws"),
            {"ws": assignment_fixture["workspace_a_id"]},
        ).one()
        assert row[0] is None
