"""Canonical competitor domains + match host binding (security E3/E4, P6-T3)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app_shared.domains import (
    canonical_domain,
    plan_competitor_domain_rewrite,
    url_host_belongs_to_domain,
)
from app_shared.enums import ProductStatus, VariantStatus
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import Competitor

from app.deps import Principal, get_current_principal
from app.main import app
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.mark.parametrize("raw", ["Amazon.sa", "amazon.sa.", "www.amazon.sa", "AMAZON.SA", " amazon.sa "])
def test_variants_collapse_to_one_spelling(raw: str) -> None:
    assert canonical_domain(raw) == "amazon.sa"


def test_idn_round_trip_is_stable() -> None:
    ascii_form = canonical_domain("Bücher.DE")
    assert ascii_form == "xn--bcher-kva.de"
    assert canonical_domain(ascii_form) == ascii_form
    assert canonical_domain("www.xn--bcher-kva.de.") == ascii_form


def test_only_a_single_leading_www_is_stripped() -> None:
    assert canonical_domain("www.www.shop.com") == "www.shop.com"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "amazon.sa:8080", "amazon.sa/x", "u@amazon.sa", "http://amazon.sa", "127.0.0.1",
     "::1", "[::1]", "2130706433", "127.1", "a b.com", "a..b.com", "amazon.sa?x=1", "-a.com"],
)
def test_non_hosts_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        canonical_domain(raw)


def test_host_binding_helper() -> None:
    assert url_host_belongs_to_domain("https://m.amazon.sa/dp/1", "amazon.sa")
    assert url_host_belongs_to_domain("https://WWW.Amazon.sa./dp/1", "amazon.sa")
    assert not url_host_belongs_to_domain("https://noon.com/x", "amazon.sa")
    assert not url_host_belongs_to_domain("https://evilamazon.sa/x", "amazon.sa")
    assert not url_host_belongs_to_domain("https://amazon.sa.evil.com/x", "amazon.sa")
    assert not url_host_belongs_to_domain("https://127.0.0.1/x", "amazon.sa")
    assert not url_host_belongs_to_domain("not a url", "amazon.sa")


def test_fair_queue_and_catalog_keys_delegate() -> None:
    from app_shared.catalog_index.keys import host_of
    from app_shared.scheduling.fair_queue import WILDCARD_DOMAIN, normalize_domain

    assert normalize_domain("WWW.Amazon.SA.") == "amazon.sa"
    assert normalize_domain(None) == WILDCARD_DOMAIN
    assert host_of("https://WWW.Amazon.sa/dp/1") == "amazon.sa"
    assert host_of("salla.sa/store") == "salla.sa"


def test_rewrite_plan_reports_collisions_and_invalid() -> None:
    ws = uuid.uuid4()
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    updates, collisions, invalid = plan_competitor_domain_rewrite(
        [(a, ws, "Amazon.sa"), (b, ws, "www.amazon.sa"), (c, ws, "noon.com"), (d, ws, "bad/host")]
    )
    assert [(u[0]) for u in updates] == [a, b]
    assert collisions == [(ws, "amazon.sa", [a, b])]
    assert invalid == [(d, "bad/host")]


# --- API ---------------------------------------------------------------------


class _UniqueSession(FakeOrmSession):
    """Fake session that enforces unique(workspace_id, domain) on flush."""

    def flush(self) -> None:
        seen: set[tuple[object, str]] = set()
        for row in self._rows.get(Competitor, []):
            key = (row.workspace_id, row.domain)
            if key in seen:
                raise IntegrityError("insert", {}, Exception("unique(workspace_id, domain)"))
            seen.add(key)
        super().flush()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def session() -> FakeOrmSession:
    return _UniqueSession()


@pytest.fixture()
def client(session: FakeOrmSession) -> TestClient:
    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key", id=uuid.uuid4(), role=None,
            scopes=["competitors:read", "competitors:write", "matches:read", "matches:write"],
            workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep
    return TestClient(app)


def test_create_stores_canonical_domain(client: TestClient) -> None:
    resp = client.post("/v1/competitors", json={"name": "A", "domain": "WWW.Amazon.SA."})
    assert resp.status_code == 201
    assert resp.json()["domain"] == "amazon.sa"


def test_duplicate_after_canonicalisation_is_409(client: TestClient) -> None:
    assert client.post("/v1/competitors", json={"name": "A", "domain": "amazon.sa"}).status_code == 201
    resp = client.post("/v1/competitors", json={"name": "B", "domain": "Amazon.sa"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_DOMAIN"


def test_invalid_domain_is_422(client: TestClient) -> None:
    resp = client.post("/v1/competitors", json={"name": "A", "domain": "amazon.sa/x"})
    assert resp.status_code == 422


def _seed(session: FakeOrmSession) -> tuple[ProductVariant, Competitor]:
    product_id = uuid.uuid4()
    session.seed(Product(id=product_id, workspace_id=WORKSPACE_ID, title="W", status=ProductStatus.ACTIVE))
    variant = ProductVariant(
        id=uuid.uuid4(), workspace_id=WORKSPACE_ID, product_id=product_id,
        external_id="v1", title="D", current_price=Decimal("1"), currency="USD",
        status=VariantStatus.ACTIVE,
    )
    session.seed(variant)
    comp = Competitor(id=uuid.uuid4(), workspace_id=WORKSPACE_ID, name="Amazon", domain="amazon.sa")
    session.seed(comp)
    return variant, comp


def _post(client: TestClient, variant: ProductVariant, comp: Competitor, url: str):
    return client.post(
        "/v1/matches",
        json={"variant_external_id": variant.external_id, "competitor_id": str(comp.id), "competitor_url": url},
    )


def test_match_on_foreign_host_is_422(client: TestClient, session: FakeOrmSession) -> None:
    variant, comp = _seed(session)
    resp = _post(client, variant, comp, "https://noon.com/p/1")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MATCH_HOST_NOT_COMPETITOR"


def test_match_on_subdomain_is_201(client: TestClient, session: FakeOrmSession) -> None:
    variant, comp = _seed(session)
    assert _post(client, variant, comp, "https://m.amazon.sa/dp/1").status_code == 201


# --- E3 on POST /v1/matches/bulk-upsert (P6 review finding 1) --------------


class _BulkSession(_UniqueSession):
    """Adds just enough of the set-based upsert for bulk-upsert to run."""

    def __init__(self) -> None:
        super().__init__()
        self.upserted_rows: list[dict] = []

    def execute(self, stmt):  # type: ignore[override]
        from sqlalchemy.sql.dml import Insert

        if isinstance(stmt, Insert):
            from app_shared.models.competitors_matches import CompetitorProductMatch

            rows = [
                {col.key if hasattr(col, "key") else col: val for col, val in row.items()}
                for row in stmt._multi_values[0]
            ]
            ids = []
            for row in rows:
                self.upserted_rows.append(row)
                match = CompetitorProductMatch(id=uuid.uuid4(), **row)
                self.add(match)
                ids.append(match)
            self.flush()
            from types import SimpleNamespace

            class _R:
                def all(self_inner):
                    return [SimpleNamespace(id=m.id) for m in ids]

            return _R()
        return super().execute(stmt)


def _bulk(client: TestClient, variant: ProductVariant, comp: Competitor, urls: list[str]):
    return client.post(
        "/v1/matches/bulk-upsert",
        json={
            "matches": [
                {"variant_external_id": variant.external_id, "competitor_id": str(comp.id), "competitor_url": url}
                for url in urls
            ]
        },
    )


def test_bulk_upsert_rejects_foreign_host_and_keeps_subdomain() -> None:
    session = _BulkSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key", id=uuid.uuid4(), role=None,
            scopes=["matches:read", "matches:write"], workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep
    variant, comp = _seed(session)
    resp = _bulk(
        TestClient(app), variant, comp,
        ["https://noon.com/p/1", "https://m.amazon.sa/dp/2"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rejected"] == [
        {
            "index": 0,
            "code": "MATCH_HOST_NOT_COMPETITOR",
            "reason": "host_not_competitor_domain",
            "url": "https://noon.com/p/1",
        }
    ]
    assert body["upserted"] == 1
    assert [row["competitor_url"] for row in session.upserted_rows] == ["https://m.amazon.sa/dp/2"]


def test_bulk_upsert_all_foreign_writes_nothing() -> None:
    session = _BulkSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key", id=uuid.uuid4(), role=None,
            scopes=["matches:read", "matches:write"], workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep
    variant, comp = _seed(session)
    resp = _bulk(TestClient(app), variant, comp, ["https://evil.example/p", "https://amazon.sa.evil.example/x"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upserted"] == 0
    assert [r["index"] for r in body["rejected"]] == [0, 1]
    assert {r["code"] for r in body["rejected"]} == {"MATCH_HOST_NOT_COMPETITOR"}
    assert session.upserted_rows == []
