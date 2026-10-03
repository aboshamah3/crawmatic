"""The two workspace-addressed catalog index routes against a real Postgres.

`GET  /v1/admin/index/workspaces/{id}/candidates` and
`POST /v1/admin/index/workspaces/{id}/matches` run on the real BYPASSRLS
auth session (`provisioned` points `AUTH_DATABASE_URL` at `crawmatic_auth`);
only the bearer-token check is overridden.

    docker run -d --rm --name cm-index-test --tmpfs /var/lib/postgresql \\
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55447:5432 postgres:18-alpine
    RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55447/crawmatic \\
      .venv/bin/pytest tests/integration/test_catalog_index_workspace_live.py -q -p no:cacheprovider
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from integration.test_rls_cross_workspace import provisioned  # noqa: F401 - pytest fixture

from app_shared.enums import (
    CompetitorStatus,
    MatchStatus,
    ProductStatus,
    VariantStatus,
    WorkspaceStatus,
)
from app_shared.models import Workspace
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.url_pattern import derive_match_url_fields

from app.main import app
from app.service_auth import require_service_token

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import load_catalog_index as loader  # noqa: E402

pytestmark = pytest.mark.integration

EAN = "3337875597296"
TITLE = "CeraVe Hydrating Cleanser 473 ml"
VERDICTS = {
    "myshop.example": "pool", "a.example": "pool", "b.example": "own_brand",
    "rejected.example": "pool", "removed.example": "pool",
}
#: Every `Settings` field without a default (same shape as
#: `tests/unit/test_api_thread_pool.py::_REQUIRED_ENV`). Only the auth session
#: touches a database here; the rest are never dialled.
REQUIRED_ENV = {
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "SCRAPYD_HTTP_URLS": "http://127.0.0.1:1",
    "SCRAPYD_BROWSER_URLS": "http://127.0.0.1:1",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}
# (domain, title, sku, mpn, gtin, brand, price, currency, url)
ROWS = [
    ("myshop.example", TITLE, EAN, "", "", "CeraVe", 50.0, "SAR", "https://myshop.example/p/cerave"),
    ("a.example", TITLE, EAN, "", "", "CeraVe", 55.0, "SAR", "https://a.example/p/1"),
    ("b.example", "سيرافي غسول مرطب", "", "", EAN, "", 49.0, "SAR", "https://b.example/p/1"),
    ("rejected.example", TITLE, EAN, "", "", "CeraVe", 40.0, "SAR", "https://rejected.example/p/1"),
    ("removed.example", TITLE, EAN, "", "", "CeraVe", 41.0, "SAR", "https://removed.example/p/1"),
]


def _sqlite(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE products(domain TEXT, title TEXT, sku TEXT, mpn TEXT, gtin TEXT, "
                "brand TEXT, price REAL, currency TEXT, url TEXT, available INTEGER, code_key TEXT)")
    con.executemany(
        "INSERT INTO products(domain, title, sku, mpn, gtin, brand, price, currency, url, available) "
        "VALUES (?,?,?,?,?,?,?,?,?,1)", ROWS)
    con.commit()
    con.close()
    return path


@pytest.fixture()
def world(provisioned, tmp_path, monkeypatch) -> Iterator[dict]:  # noqa: F811
    from app_shared.config import get_settings

    for name, value in {"DATABASE_URL": provisioned["app_url"], **REQUIRED_ENV}.items():
        if not os.environ.get(name) or name == "DATABASE_URL":
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    owner = create_engine(provisioned["admin_url"])
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE catalog_index_products, catalog_index_codes, catalog_index_loads"))
    pg_url = provisioned["admin_url"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(pg_url) as conn:
        loader.load(conn, _sqlite(tmp_path / "products.sqlite"), VERDICTS, allow_small=True)

    unique = uuid.uuid4().hex[:8]
    with Session(owner) as session:
        ws = Workspace(name=f"index {unique}", slug=f"index-{unique}", status=WorkspaceStatus.ACTIVE)
        session.add(ws)
        session.flush()
        product = Product(workspace_id=ws.id, title=TITLE, brand="CeraVe",
                          url="https://myshop.example/p/cerave", status=ProductStatus.ACTIVE)
        session.add(product)
        session.flush()
        variant = ProductVariant(workspace_id=ws.id, product_id=product.id, title="Default",
                                 barcode=EAN, current_price=Decimal("50.0000"), currency="SAR",
                                 status=VariantStatus.ACTIVE)
        session.add(variant)
        session.flush()
        # The merchant rejected rejected.example's listing earlier (ARCHIVED
        # match) and removed removed.example as a competitor altogether.
        rejected = Competitor(workspace_id=ws.id, name="rejected.example", domain="rejected.example")
        removed = Competitor(workspace_id=ws.id, name="removed.example", domain="removed.example",
                             status=CompetitorStatus.ARCHIVED)
        session.add_all([rejected, removed])
        session.flush()
        normalized, pattern, version = derive_match_url_fields("https://rejected.example/p/1")
        session.add(CompetitorProductMatch(
            workspace_id=ws.id, product_id=product.id, product_variant_id=variant.id,
            competitor_id=rejected.id, competitor_url="https://rejected.example/p/1",
            normalized_competitor_url=normalized, url_pattern=pattern, url_pattern_version=version,
            status=MatchStatus.ARCHIVED,
        ))
        session.commit()
        ids = {"workspace": ws.id, "product": product.id, "variant": variant.id}

    app.dependency_overrides[require_service_token] = lambda: None
    yield {"client": TestClient(app), "owner": owner, **ids}
    app.dependency_overrides.clear()
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE catalog_index_products, catalog_index_codes, catalog_index_loads"))
    owner.dispose()
    get_settings.cache_clear()


def _candidates(world, **params) -> dict:
    resp = world["client"].get(
        f"/v1/admin/index/workspaces/{world['workspace']}/candidates", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _accept(world, candidates: list[dict]) -> dict:
    resp = world["client"].post(
        f"/v1/admin/index/workspaces/{world['workspace']}/matches", json={"candidates": candidates})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _matches(world) -> dict[str, MatchStatus]:
    with Session(world["owner"]) as session:
        rows = session.execute(
            select(CompetitorProductMatch.competitor_url, CompetitorProductMatch.status).where(
                CompetitorProductMatch.workspace_id == world["workspace"])).all()
    return dict(rows)


def test_candidates_skip_own_store_rejected_listings_and_removed_competitors(world):
    body = _candidates(world)
    got = {c["domain"]: c for c in body["candidates"]}
    assert set(got) == {"a.example", "b.example"}
    assert got["b.example"]["stage"] == "barcode" and got["b.example"]["store_verdict"] == "own_brand"
    assert all(c["product_variant_id"] == str(world["variant"]) for c in body["candidates"])
    assert body["variants_scanned"] == 1 and body["next_cursor"] is None


def test_accepted_candidates_land_paused_and_never_come_back(world):
    urls = [c["url"] for c in _candidates(world)["candidates"]]
    body = _accept(world, [{"product_variant_id": str(world["variant"]), "url": u} for u in urls])
    assert body["created"] == 2 and body["competitors_created"] == 2 and body["rejected"] == []
    matches = _matches(world)
    assert {matches[u] for u in urls} == {MatchStatus.PAUSED}
    assert _candidates(world)["candidates"] == []          # nothing is offered (or billed) twice


def test_accepting_again_never_touches_an_existing_match(world):
    candidate = {"product_variant_id": str(world["variant"]), "url": "https://rejected.example/p/1"}
    body = _accept(world, [candidate])
    assert body["created"] == 0 and body["rejected"] == []
    assert _matches(world)["https://rejected.example/p/1"] == MatchStatus.ARCHIVED


def test_bad_rows_are_rejected_one_by_one(world):
    variant = str(world["variant"])
    body = _accept(world, [
        {"product_variant_id": str(uuid.uuid4()), "url": "https://a.example/p/1"},
        {"product_variant_id": variant, "url": "https://removed.example/p/1"},
        {"product_variant_id": variant, "url": "http://127.0.0.1/admin"},
        {"product_variant_id": variant, "url": "https://b.example/p/1"},
    ])
    assert sorted((r["index"], r["code"]) for r in body["rejected"]) == [
        (0, "UNRESOLVED_VARIANT"), (1, "COMPETITOR_ARCHIVED"), (2, "UNSAFE_URL")]
    assert body["created"] == 1


def _competitor_hosts(world) -> set[str]:
    with Session(world["owner"]) as session:
        return set(session.execute(select(Competitor.domain).where(
            Competitor.workspace_id == world["workspace"])).scalars())


@pytest.fixture()
def cap_of_one(monkeypatch):
    """The protected-link cap with room for exactly one more link per
    product: the real check, reduced to its decision."""
    from fastapi import HTTPException

    import app.routers.catalog_index as routes

    def check(_session, _workspace_id, rows):
        if len(rows) > 1:
            raise HTTPException(status_code=422, detail={"error": {"code": "PROTECTED_LINK_CAP_REACHED"}})

    monkeypatch.setattr(routes, "_bulk_protected_cap_check", check)


def test_candidates_the_cap_would_reject_are_never_offered(world, cap_of_one):
    # Review 2026-10-02: a row the accept refuses used to come back, and be
    # billed, on every discovery pass. The candidates route now dry-runs it.
    before = _competitor_hosts(world)
    body = _candidates(world)
    assert len(body["candidates"]) == 1
    assert _competitor_hosts(world) == before        # the dry run left nothing behind


def test_accept_keeps_what_fits_under_the_cap_and_rejects_the_rest(world, cap_of_one):
    variant = str(world["variant"])
    body = _accept(world, [
        {"product_variant_id": variant, "url": "https://a.example/p/1"},
        {"product_variant_id": variant, "url": "https://b.example/p/1"},
    ])
    assert body["created"] == 1
    assert [(r["index"], r["code"]) for r in body["rejected"]] == [(1, "PROTECTED_LINK_CAP_REACHED")]


def test_a_removed_competitor_stored_with_www_stays_removed(world):
    with Session(world["owner"]) as session:
        session.add(Competitor(workspace_id=world["workspace"], name="a.example",
                               domain="www.a.example", status=CompetitorStatus.ARCHIVED))
        session.commit()
    assert "a.example" not in {c["domain"] for c in _candidates(world)["candidates"]}
    body = _accept(world, [{"product_variant_id": str(world["variant"]), "url": "https://a.example/p/1"}])
    assert [r["code"] for r in body["rejected"]] == ["COMPETITOR_ARCHIVED"] and body["created"] == 0


def test_an_unknown_workspace_is_404(world):
    resp = world["client"].get(f"/v1/admin/index/workspaces/{uuid.uuid4()}/candidates")
    assert resp.status_code == 404
