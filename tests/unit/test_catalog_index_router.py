"""`/v1/admin/index`: auth seams and the lookup contract (no database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app_shared.catalog_index.lookup import IndexCandidate

from app.main import app
from app.openapi_public import build_public_openapi
from app.routers import catalog_index

INDEX = {"Authorization": "Bearer index-token"}
SAAS = {"Authorization": "Bearer saas-token"}
WS = uuid.uuid4()
CANDIDATE = IndexCandidate(
    domain="a.example", host="a.example", url="https://a.example/p/1", title="CeraVe Cleanser",
    brand="CeraVe", price=Decimal("55.00"), currency="SAR", available=True,
    observed_at=datetime(2026, 10, 2, tzinfo=timezone.utc), store_verdict="pool",
    stage="barcode", score=1.0, matched_key="03337875597296",
)


def _use_tokens(monkeypatch, *, index: str | None, saas: str | None) -> None:
    """The unit suite has no usable `Settings`; stub the two seams that
    read tokens, the way `test_service_auth.py` does."""
    settings = SimpleNamespace(INDEX_SERVICE_TOKEN=index, SAAS_SERVICE_TOKEN=saas)
    monkeypatch.setattr("app.index_auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.service_auth.get_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _tokens(monkeypatch) -> Iterator[None]:
    _use_tokens(monkeypatch, index="index-token", saas="saas-token")
    app.dependency_overrides[catalog_index.get_catalog_index_session] = lambda: object()
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def calls(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def fake_lookup(session, query, **kwargs):
        seen.append({"query": query, **kwargs})
        return [CANDIDATE]

    monkeypatch.setattr(catalog_index, "active_generation", lambda session: 7)
    monkeypatch.setattr(catalog_index, "lookup", fake_lookup)
    return seen


BODY = {"products": [{"ref": "p1", "title": "CeraVe Cleanser", "gtin": "3337875597296"}]}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/admin/index/status"),
        ("post", "/v1/admin/index/lookup"),
        ("get", f"/v1/admin/index/workspaces/{WS}/candidates"),
        ("post", f"/v1/admin/index/workspaces/{WS}/matches"),
    ],
)
def test_every_route_refuses_a_missing_or_wrong_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_the_index_token_cannot_reach_the_workspace_routes(client):
    assert client.get(f"/v1/admin/index/workspaces/{WS}/candidates", headers=INDEX).status_code == 401
    resp = client.post(
        f"/v1/admin/index/workspaces/{WS}/matches",
        headers=INDEX,
        json={"candidates": [{"product_variant_id": str(uuid.uuid4()), "url": "https://a.example/p"}]},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("headers", [INDEX, SAAS])
def test_lookup_accepts_either_token_and_returns_candidates_per_ref(client, calls, headers):
    resp = client.post("/v1/admin/index/lookup", headers=headers, json={
        **BODY, "exclude_domains": ["myshop.example"], "limit_per_product": 3, "pool_only": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation"] == 7
    assert body["results"][0]["ref"] == "p1"
    candidate = body["results"][0]["candidates"][0]
    assert candidate["url"] == "https://a.example/p/1" and candidate["stage"] == "barcode"
    assert candidate["price"] == "55.00" and candidate["observed_at"].startswith("2026-10-02")
    assert calls[0]["query"].gtin == "3337875597296"
    assert calls[0]["exclude_domains"] == ["myshop.example"]
    assert calls[0]["limit"] == 3 and calls[0]["pool_only"] is False and calls[0]["generation"] == 7


def test_lookup_with_no_active_generation_returns_empty_candidates(client, monkeypatch):
    monkeypatch.setattr(catalog_index, "active_generation", lambda session: None)
    monkeypatch.setattr(catalog_index, "lookup", lambda *a, **k: pytest.fail("must not query"))
    resp = client.post("/v1/admin/index/lookup", headers=INDEX, json=BODY)
    assert resp.status_code == 200
    assert resp.json() == {"generation": None, "results": [{"ref": "p1", "candidates": []}]}


def test_lookup_refuses_oversized_and_malformed_batches(client, calls):
    too_many = {"products": [{"ref": str(i), "title": "x"} for i in range(51)]}
    assert client.post("/v1/admin/index/lookup", headers=INDEX, json=too_many).status_code == 422
    assert client.post("/v1/admin/index/lookup", headers=INDEX, json={"products": []}).status_code == 422
    unknown = {"products": [{"ref": "p1", "title": "x", "surprise": 1}]}
    assert client.post("/v1/admin/index/lookup", headers=INDEX, json=unknown).status_code == 422


def test_no_configured_token_fails_closed(client, monkeypatch):
    _use_tokens(monkeypatch, index=None, saas=None)
    assert client.post("/v1/admin/index/lookup", headers=INDEX, json=BODY).status_code == 401


def test_index_routes_are_absent_from_the_public_openapi():
    assert not [p for p in build_public_openapi(app)["paths"] if "/index" in p]


# --- risk review 2026-10-06 (P7): title-stage ordering, candidates work cap ---


def test_title_stage_ranks_by_ts_rank_before_the_scan_limit():
    """Without an ORDER BY the `LIMIT :scan` kept an arbitrary 200 of a
    common phrase's matches, so the best titles could be cut before the
    Jaccard re-rank ever saw them."""
    from app_shared.catalog_index import lookup as lookup_module

    sql = " ".join(lookup_module._TITLE_SQL.text.split())
    order = sql.index("ORDER BY ts_rank(")
    assert order < sql.index("LIMIT :scan")
    ranked = sql[order:sql.index("LIMIT :scan")]
    assert "plainto_tsquery('simple', :query_text)) DESC" in ranked
    assert "to_tsvector('simple', coalesce(p.title, ''))" in ranked
    # Deterministic tie-break, the same as the re-rank's own.
    assert ranked.rstrip().endswith("p.price ASC, p.domain ASC")


class _CandidatesSession:
    """`FakeAlertsListSession` plus `get()` for `_require_workspace`."""

    def __init__(self):
        from unit._alerts_list_fake_session import FakeAlertsListSession

        self.inner = FakeAlertsListSession()

    def get(self, model, ident):
        return object()

    def execute(self, stmt):
        return self.inner.execute(stmt)


def _seed_variants(n: int) -> tuple[_CandidatesSession, list[uuid.UUID]]:
    from app_shared.enums import VariantStatus
    from app_shared.models.catalog import Product, ProductVariant

    session = _CandidatesSession()
    ids = []
    for i in range(n):
        product = Product(workspace_id=WS, title=f"Product {i}", brand="Brand")
        product.id = uuid.uuid4()
        variant = ProductVariant(
            workspace_id=WS, product_id=product.id, title=f"Variant {i}",
            status=VariantStatus.ACTIVE,
        )
        variant.id = uuid.uuid4()
        session.inner.seed(product, variant)
        ids.append(variant.id)
    return session, sorted(ids)


def test_candidates_route_caps_work_at_50_variants_and_returns_the_rest_by_cursor(
    client, monkeypatch
):
    session, ids = _seed_variants(120)
    looked_up: list[str] = []
    monkeypatch.setattr(catalog_index, "active_generation", lambda s: 7)
    monkeypatch.setattr(
        catalog_index, "lookup", lambda s, query, **kw: looked_up.append(query.title) or []
    )
    app.dependency_overrides[catalog_index.get_catalog_index_session] = lambda: session
    path = f"/v1/admin/index/workspaces/{WS}/candidates"

    # Even when the caller asks for more, one request does at most 50 lookups.
    first = client.get(path, headers=SAAS, params={"variant_limit": 200}).json()
    assert first["variants_scanned"] == 50 and len(looked_up) == 50
    assert first["next_cursor"] == str(ids[49])

    second = client.get(path, headers=SAAS, params={"cursor": first["next_cursor"]}).json()
    assert second["variants_scanned"] == 50 and second["next_cursor"] == str(ids[99])

    third = client.get(path, headers=SAAS, params={"cursor": second["next_cursor"]}).json()
    assert third["variants_scanned"] == 20 and third["next_cursor"] is None
    assert len(looked_up) == 120
