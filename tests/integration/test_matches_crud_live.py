"""Live-Postgres match CRUD test (SPEC-05 US2 T021, SC-002/003/004) — ⏸ DEFERRED.

Exercises the full `/v1/matches` surface against a real database through
FastAPI's `TestClient` (no running server/container required — only the
database needs to be live):

1. Create a match with a safe public http(s) URL -> stored with
   `normalized_competitor_url` + `url_pattern` + `url_pattern_version`,
   `product_id` derived from the variant's parent (FR-004/FR-010/FR-011,
   SC-002, research D4).
2. Submit an unsafe URL (localhost / private / metadata / userinfo /
   non-http scheme) on create -> `422 UNSAFE_URL`, not stored
   (FR-007/008/009, SC-004).
3. A variant can hold unlimited matches across different competitors and
   URLs; only the exact `(variant, competitor, normalized URL)` tuple
   duplicates -> `409 DUPLICATE_MATCH` (FR-005, SC-003).
4. Two raw URLs that normalize to the same `normalized_competitor_url`
   collide on the unique key (dup -> `409`).
5. Read / update (re-validates + re-derives on `competitor_url` change) /
   list / delete round-trip (FR-016).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-05 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `competitor_product_matches` table doesn't exist yet (mirrors
`tests/integration/test_competitors_crud_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-05 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_matches_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-05 `competitor_product_matches` table already exists (migration
    applied)."""
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
        if not {"competitors", "competitor_product_matches"} <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_matches_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-05 "
        "competitor_product_matches migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A fresh ACTIVE workspace + a full-scoped catalog/competitors/matches
    API key, plus one persisted product+variant, cleaned up after."""
    from decimal import Decimal

    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Matches Live Test {unique}",
            slug=f"matches-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        product = Product(
            workspace_id=workspace_id,
            title="Matches Live Test Product",
            status=ProductStatus.ACTIVE,
        )
        session.add(product)
        session.flush()

        variant = ProductVariant(
            workspace_id=workspace_id,
            product_id=product.id,
            title="Default",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add(variant)
        session.flush()
        variant_id = variant.id
        product_id = product.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="matches-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=[
                "competitors:read",
                "competitors:write",
                "matches:read",
                "matches:write",
            ],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {
        "workspace_id": str(workspace_id),
        "api_key": full_secret,
        "product_id": str(product_id),
        "product_variant_id": str(variant_id),
    }

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM product_variants WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


@pytest.fixture()
def competitor_id(client, auth_headers, workspace_and_api_key) -> str:
    unique = uuid.uuid4().hex[:8]
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={"name": "Live Test Competitor", "domain": f"competitor-{unique}.example.com"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- SC-002: create with a safe URL -> normalized + pattern + version ------


def test_create_match_with_safe_url_stores_normalized_and_pattern(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    response = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": workspace_and_api_key["product_variant_id"],
            "competitor_id": competitor_id,
            "competitor_url": "https://WWW.Competitor.com/ar/products/iphone-15/?utm=x#frag",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["product_id"] == workspace_and_api_key["product_id"]
    assert body["product_variant_id"] == workspace_and_api_key["product_variant_id"]
    assert body["normalized_competitor_url"] == (
        "https://competitor.com/ar/products/iphone-15?utm=x"
    )
    assert body["url_pattern"] == "competitor.com/ar/products/*"
    assert body["url_pattern_version"] == 1
    assert body["health_status"] == "UNKNOWN"
    assert body["consecutive_failures"] == 0


# --- SC-004: unsafe URLs rejected at save time, never stored ---------------


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://user:pass@competitor.com/",
        "ftp://competitor.com/",
    ],
)
def test_create_match_with_unsafe_url_is_422_and_not_stored(
    client, auth_headers, workspace_and_api_key, competitor_id, unsafe_url
) -> None:
    response = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": workspace_and_api_key["product_variant_id"],
            "competitor_id": competitor_id,
            "competitor_url": unsafe_url,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "UNSAFE_URL"

    listing = client.get("/v1/matches", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["items"] == []


# --- SC-003: unlimited matches per variant; exact-tuple dup rejected -------


def test_variant_can_hold_many_matches_across_competitors_and_urls(
    client, auth_headers, workspace_and_api_key
) -> None:
    variant_id = workspace_and_api_key["product_variant_id"]

    created = []
    for i in range(3):
        comp = client.post(
            "/v1/competitors",
            headers=auth_headers,
            json={
                "name": f"Comp {i}",
                "domain": f"comp-{uuid.uuid4().hex[:8]}.example.com",
            },
        )
        assert comp.status_code == 201
        resp = client.post(
            "/v1/matches",
            headers=auth_headers,
            json={
                "product_variant_id": variant_id,
                "competitor_id": comp.json()["id"],
                "competitor_url": f"https://comp-{i}.example.com/p/{i}",
            },
        )
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["id"])

    assert len(set(created)) == 3


def test_exact_tuple_duplicate_match_is_409(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["product_variant_id"]
    url = "https://competitor.com/p/dup-1"

    first = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": url,
        },
    )
    assert first.status_code == 201

    second = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": url,
        },
    )
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "DUPLICATE_MATCH"


def test_two_raw_urls_normalizing_equal_collide_as_duplicate(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["product_variant_id"]

    first = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": "https://www.competitor.com/p/norm-eq/",
        },
    )
    assert first.status_code == 201

    second = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": "HTTPS://competitor.com/p/norm-eq#frag",
        },
    )
    assert second.status_code == 409


# --- read / update / list / delete ------------------------------------------


def test_read_update_list_delete_round_trip(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["product_variant_id"]

    create = client.post(
        "/v1/matches",
        headers=auth_headers,
        json={
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": "https://competitor.com/p/roundtrip",
        },
    )
    assert create.status_code == 201
    match_id = create.json()["id"]

    # Read.
    read = client.get(f"/v1/matches/{match_id}", headers=auth_headers)
    assert read.status_code == 200

    # Update (URL change -> re-validate + re-derive).
    update = client.patch(
        f"/v1/matches/{match_id}",
        headers=auth_headers,
        json={"competitor_url": "https://competitor.com/p/roundtrip-updated"},
    )
    assert update.status_code == 200
    assert update.json()["normalized_competitor_url"] == (
        "https://competitor.com/p/roundtrip-updated"
    )
    assert update.json()["url_pattern"] == "competitor.com/p/*"

    # Update with an unsafe URL -> 422, previous state untouched.
    bad_update = client.patch(
        f"/v1/matches/{match_id}",
        headers=auth_headers,
        json={"competitor_url": "http://localhost/"},
    )
    assert bad_update.status_code == 422
    assert bad_update.json()["detail"]["error"]["code"] == "UNSAFE_URL"

    # List includes it.
    listing = client.get("/v1/matches", headers=auth_headers)
    assert listing.status_code == 200
    assert any(item["id"] == match_id for item in listing.json()["items"])

    # Delete.
    delete = client.delete(f"/v1/matches/{match_id}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": match_id, "outcome": "hard_deleted"}

    # Subsequent read 404s.
    after = client.get(f"/v1/matches/{match_id}", headers=auth_headers)
    assert after.status_code == 404
