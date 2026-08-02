"""Bulk price-comparison + per-competitor-prices router unit tests
(WooCommerce-plugin backend gaps, PLAN_WOOCOMMERCE_PLUGIN.md §6 items 1-2).

Two new read routes on `apps/api/app/routers/variants.py`, exercised the
same DB-less way as `tests/unit/test_alerts_router.py`: `TestClient` with
`app.dependency_overrides[get_current_principal]` swapped for a fake
principal bound to `FakeAlertsListSession`
(`tests/unit/_alerts_list_fake_session.py` — the existing
`Select`-evaluating double that understands `ORDER BY`/`LIMIT`/`.in_()`
and the `tuple_(...) > tuple_(...)` keyset predicate, which is exactly
what both routes issue).

* `GET /v1/variants/price-comparison` — cursor-paginated
  `{items, next_cursor}` over `variant_price_states`. Includes the
  route-ordering regression guard: this STATIC path must not be swallowed
  by the DYNAMIC `GET /v1/variants/{variant_id}` registered after it.
* `GET /v1/variants/{variant_id}/competitor-prices` — cursor-paginated
  `{items, next_cursor}`, one item per match of the variant, stitched to
  its `match_current_prices` row and its competitor's name; `404` only
  for an unknown/cross-workspace variant, an empty envelope for a variant
  with no matches.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app_shared.enums import (
    AlertSeverity,
    AlertType,
    HealthStatus,
    MatchPriority,
    MatchStatus,
    StockStatus,
)
from app_shared.models.alerts import VariantPriceState
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.observations import MatchCurrentPrice
from app_shared.pagination import MAX_LIMIT

from app.deps import Principal, get_current_principal
from app.main import app

from unit._alerts_list_fake_session import FakeAlertsListSession

# The route-introspection helpers live in `test_catalog_scope_gating` (the
# module that owns the scope-gating registry) — imported, never re-copied,
# so the two modules can never disagree about how a route's declared
# `require_scopes(...)` is read.
from unit.test_catalog_scope_gating import _iter_api_routes, _required_scopes, _route

WORKSPACE_ID = uuid.uuid4()
OTHER_WORKSPACE_ID = uuid.uuid4()


def _override_principal(
    session: FakeAlertsListSession,
    *,
    scopes: list[str],
    workspace_id: uuid.UUID = WORKSPACE_ID,
):
    def _dependency() -> Iterator[tuple[FakeAlertsListSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=workspace_id,
        )

    return _dependency


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def session() -> FakeAlertsListSession:
    return FakeAlertsListSession()


# --- row builders ------------------------------------------------------------


def _make_variant(*, workspace_id: uuid.UUID = WORKSPACE_ID) -> ProductVariant:
    now = datetime.now(timezone.utc)
    variant = ProductVariant(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        title="Widget",
        current_price=Decimal("2999.0000"),
        currency="SAR",
        status="active",
        created_at=now,
        updated_at=now,
    )
    variant.id = uuid.uuid4()
    return variant


def _make_price_state(
    *,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    variant_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> VariantPriceState:
    now = created_at or datetime.now(timezone.utc)
    state = VariantPriceState(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id or uuid.uuid4(),
        client_price=Decimal("2999.0000"),
        currency="SAR",
        cheapest_competitor_price=Decimal("2799.0000"),
        average_competitor_price=Decimal("2899.0000"),
        highest_competitor_price=Decimal("3099.0000"),
        comparable_competitor_count=3,
        latest_alert_type=AlertType.HIGH_PRICE,
        latest_alert_severity=AlertSeverity.HIGH,
        calculated_at=now,
        created_at=now,
        updated_at=now,
    )
    state.id = uuid.uuid4()
    return state


def _seed_analyzed_variant(
    session: FakeAlertsListSession,
    *,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    created_at: datetime | None = None,
) -> VariantPriceState:
    """Seed a `variant_price_states` row **and** the `product_variants` row it
    points at — the only combination the bulk route returns (it filters out
    price states orphaned by a product delete)."""
    variant = _make_variant(workspace_id=workspace_id)
    state = _make_price_state(
        workspace_id=workspace_id, variant_id=variant.id, created_at=created_at
    )
    session.seed(variant, state)
    return state


def _make_competitor(
    *, name: str, workspace_id: uuid.UUID = WORKSPACE_ID
) -> Competitor:
    now = datetime.now(timezone.utc)
    competitor = Competitor(
        workspace_id=workspace_id,
        name=name,
        domain=f"{name.lower().replace(' ', '-')}.example",
        created_at=now,
        updated_at=now,
    )
    competitor.id = uuid.uuid4()
    return competitor


def _make_match(
    *,
    variant_id: uuid.UUID,
    competitor: Competitor,
    url: str,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    health_status: HealthStatus = HealthStatus.HEALTHY,
    created_at: datetime | None = None,
) -> CompetitorProductMatch:
    now = created_at or datetime.now(timezone.utc)
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id,
        competitor_id=competitor.id,
        competitor_url=url,
        normalized_competitor_url=url,
        url_pattern=url,
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
        health_status=health_status,
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )
    match.id = uuid.uuid4()
    return match


def _make_current_price(
    *,
    match: CompetitorProductMatch,
    price: Decimal,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    scraped_at: datetime | None = None,
) -> MatchCurrentPrice:
    now = datetime.now(timezone.utc)
    current = MatchCurrentPrice(
        workspace_id=workspace_id,
        match_id=match.id,
        product_id=match.product_id,
        product_variant_id=match.product_variant_id,
        competitor_id=match.competitor_id,
        price=price,
        currency="SAR",
        stock_status=StockStatus.IN_STOCK,
        comparable=True,
        success=True,
        scraped_at=scraped_at or now,
        created_at=now,
        updated_at=now,
    )
    current.id = uuid.uuid4()
    return current


# --- GET /v1/variants/price-comparison ---------------------------------------


def test_bulk_price_comparison_returns_200_shape(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    state = _seed_analyzed_variant(session)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["product_variant_id"] == str(state.product_variant_id)
    assert item["currency"] == "SAR"
    # Raw serialized string, not a Decimal-coerced comparison: the wire
    # format (scale included) is part of the contract.
    assert item["client_price"] == "2999.0000"
    assert Decimal(item["cheapest_competitor_price"]) == Decimal("2799.0000")
    assert Decimal(item["average_competitor_price"]) == Decimal("2899.0000")
    assert Decimal(item["highest_competitor_price"]) == Decimal("3099.0000")
    assert item["comparable_competitor_count"] == 3
    assert item["alert_type"] == "HIGH_PRICE"
    assert item["alert_severity"] == "HIGH"
    assert item["calculated_at"] is not None


def test_bulk_price_comparison_threads_cursor_across_pages(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    base = datetime.now(timezone.utc)
    states = [
        _seed_analyzed_variant(session, created_at=base + timedelta(seconds=i)) for i in range(3)
    ]
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/variants/price-comparison", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    assert [i["product_variant_id"] for i in body["items"]] == [
        str(states[0].product_variant_id),
        str(states[1].product_variant_id),
    ]

    resp2 = client.get(
        "/v1/variants/price-comparison",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None
    assert body2["items"][0]["product_variant_id"] == str(states[2].product_variant_id)


def test_bulk_price_comparison_excludes_other_workspaces(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    mine = _seed_analyzed_variant(session)
    _seed_analyzed_variant(session, workspace_id=OTHER_WORKSPACE_ID)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 200
    body = resp.json()
    assert [i["product_variant_id"] for i in body["items"]] == [str(mine.product_variant_id)]


def test_bulk_price_comparison_excludes_orphaned_price_states(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """A price state whose `product_variant_id` no longer resolves to a row is
    never returned.

    Deleting a product hard-deletes its `product_variants` but leaves their
    `variant_price_states` behind (no FK), so the route must filter on the
    variant still existing — otherwise it would list rows that
    `GET /v1/variants/{variant_id}` 404s.
    """
    live = _seed_analyzed_variant(session)
    orphan = _make_price_state()  # deliberately: no matching ProductVariant row
    session.seed(orphan)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 200
    returned = [i["product_variant_id"] for i in resp.json()["items"]]
    assert returned == [str(live.product_variant_id)]
    assert str(orphan.product_variant_id) not in returned


def test_bulk_price_comparison_paginates_rows_sharing_a_created_at(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """Keyset tie-break: several rows with the identical `created_at` page
    through on `(created_at, id)` with no duplicates and no drops."""
    shared = datetime.now(timezone.utc)
    states = [_seed_analyzed_variant(session, created_at=shared) for _ in range(5)]
    expected = [str(s.product_variant_id) for s in sorted(states, key=lambda s: (s.created_at, s.id))]
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):  # bounded: never loop forever on a broken cursor
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = client.get("/v1/variants/price-comparison", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(i["product_variant_id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert seen == expected
    assert len(set(seen)) == len(seen)


def test_bulk_price_comparison_clamps_limit(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """`clamp_limit` floor and ceiling: `limit=0` -> 1 row, `limit=100000` ->
    `MAX_LIMIT` (never an unbounded scan, never an empty page)."""
    base = datetime.now(timezone.utc)
    for i in range(MAX_LIMIT + 1):
        _seed_analyzed_variant(session, created_at=base + timedelta(seconds=i))
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    floored = client.get("/v1/variants/price-comparison", params={"limit": 0})
    assert floored.status_code == 200
    assert len(floored.json()["items"]) == 1
    assert floored.json()["next_cursor"] is not None

    capped = client.get("/v1/variants/price-comparison", params={"limit": 100000})
    assert capped.status_code == 200
    assert len(capped.json()["items"]) == MAX_LIMIT
    assert capped.json()["next_cursor"] is not None


def test_bulk_price_comparison_empty_is_200(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


def test_bulk_price_comparison_malformed_cursor_is_422(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/variants/price-comparison", params={"cursor": "not-a-cursor!!"})

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


def test_bulk_price_comparison_missing_scope_is_403(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    _seed_analyzed_variant(session)
    app.dependency_overrides[get_current_principal] = _override_principal(session, scopes=[])

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 403


def test_bulk_price_comparison_is_not_swallowed_by_variant_id_route(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """Route-ordering regression guard (the whole reason the static route is
    registered before `GET /v1/variants/{variant_id}`).

    If `/{variant_id}` matched first, `"price-comparison"` would fail
    `uuid.UUID` parsing and the request would 422 (never reaching the list
    handler). Asserted both dynamically (a real request 200s) and
    statically (registration order in `app.routes`).
    """
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read", "variants:read"]
    )

    resp = client.get("/v1/variants/price-comparison")

    assert resp.status_code == 200, resp.json()
    assert "items" in resp.json()

    paths = [route.path for route in _iter_api_routes()]
    assert paths.index("/v1/variants/price-comparison") < paths.index("/v1/variants/{variant_id}")


# --- GET /v1/variants/{variant_id}/competitor-prices -------------------------


def test_competitor_prices_returns_200_rows(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant()
    competitor = _make_competitor(name="Competitor One")
    match = _make_match(
        variant_id=variant.id, competitor=competitor, url="https://c1.example/p/1"
    )
    scraped_at = datetime.now(timezone.utc)
    current = _make_current_price(match=match, price=Decimal("2799.0000"), scraped_at=scraped_at)
    session.seed(variant, competitor, match, current)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["match_id"] == str(match.id)
    assert row["competitor_id"] == str(competitor.id)
    assert row["competitor_name"] == "Competitor One"
    assert row["url"] == "https://c1.example/p/1"
    # Raw serialized string, not a Decimal-coerced comparison.
    assert row["price"] == "2799.0000"
    assert row["currency"] == "SAR"
    assert row["scraped_at"] is not None
    assert row["health_status"] == "HEALTHY"


def test_competitor_prices_includes_never_scraped_match_with_null_price(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant()
    base = datetime.now(timezone.utc)
    priced_competitor = _make_competitor(name="Priced")
    bare_competitor = _make_competitor(name="Bare")
    priced_match = _make_match(
        variant_id=variant.id,
        competitor=priced_competitor,
        url="https://priced.example/p/1",
        created_at=base,
    )
    bare_match = _make_match(
        variant_id=variant.id,
        competitor=bare_competitor,
        url="https://bare.example/p/1",
        health_status=HealthStatus.UNKNOWN,
        created_at=base + timedelta(seconds=1),
    )
    session.seed(
        variant,
        priced_competitor,
        bare_competitor,
        priced_match,
        bare_match,
        _make_current_price(match=priced_match, price=Decimal("10.0000")),
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [row["competitor_name"] for row in items] == ["Priced", "Bare"]
    assert Decimal(items[0]["price"]) == Decimal("10.0000")
    assert items[1]["price"] is None
    assert items[1]["currency"] is None
    assert items[1]["scraped_at"] is None
    assert items[1]["health_status"] == "UNKNOWN"


def test_competitor_prices_no_matches_is_empty_200(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


def test_competitor_prices_unknown_variant_is_404(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{uuid.uuid4()}/competitor-prices")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_competitor_prices_cross_workspace_variant_is_404(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant(workspace_id=OTHER_WORKSPACE_ID)
    competitor = _make_competitor(name="Theirs", workspace_id=OTHER_WORKSPACE_ID)
    match = _make_match(
        variant_id=variant.id,
        competitor=competitor,
        url="https://theirs.example/p/1",
        workspace_id=OTHER_WORKSPACE_ID,
    )
    session.seed(variant, competitor, match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_competitor_prices_excludes_other_workspace_match_rows(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """Same variant id, a match row owned by another workspace -> not returned."""
    variant = _make_variant()
    mine_competitor = _make_competitor(name="Mine")
    mine_match = _make_match(
        variant_id=variant.id, competitor=mine_competitor, url="https://mine.example/p/1"
    )
    theirs_competitor = _make_competitor(name="Theirs", workspace_id=OTHER_WORKSPACE_ID)
    theirs_match = _make_match(
        variant_id=variant.id,
        competitor=theirs_competitor,
        url="https://theirs.example/p/1",
        workspace_id=OTHER_WORKSPACE_ID,
    )
    session.seed(
        variant,
        mine_competitor,
        theirs_competitor,
        mine_match,
        theirs_match,
        _make_current_price(match=mine_match, price=Decimal("5.0000")),
        _make_current_price(
            match=theirs_match, price=Decimal("1.0000"), workspace_id=OTHER_WORKSPACE_ID
        ),
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [row["match_id"] for row in items] == [str(mine_match.id)]
    assert Decimal(items[0]["price"]) == Decimal("5.0000")


def test_competitor_prices_threads_cursor_across_pages(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    """`{items, next_cursor}` keyset paging over the variant's matches
    (`contracts/pagination.md`) — page 2 continues exactly where page 1
    stopped, with no overlap."""
    variant = _make_variant()
    base = datetime.now(timezone.utc)
    seeded: list[CompetitorProductMatch] = []
    for i in range(3):
        competitor = _make_competitor(name=f"Competitor {i}")
        match = _make_match(
            variant_id=variant.id,
            competitor=competitor,
            url=f"https://c{i}.example/p/1",
            created_at=base + timedelta(seconds=i),
        )
        session.seed(competitor, match, _make_current_price(match=match, price=Decimal("1.0000")))
        seeded.append(match)
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [row["match_id"] for row in body["items"]] == [
        str(seeded[0].id),
        str(seeded[1].id),
    ]
    assert body["next_cursor"] is not None

    resp2 = client.get(
        f"/v1/variants/{variant.id}/competitor-prices",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert [row["match_id"] for row in body2["items"]] == [str(seeded[2].id)]
    assert body2["next_cursor"] is None


def test_competitor_prices_malformed_cursor_is_422(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["alerts:read"]
    )

    resp = client.get(
        f"/v1/variants/{variant.id}/competitor-prices", params={"cursor": "not-a-cursor!!"}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


def test_competitor_prices_missing_scope_is_403(
    client: TestClient, session: FakeAlertsListSession
) -> None:
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(session, scopes=[])

    resp = client.get(f"/v1/variants/{variant.id}/competitor-prices")

    assert resp.status_code == 403


# --- static: declared require_scopes -----------------------------------------


def test_bulk_price_comparison_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/variants/price-comparison", "GET")
    assert _required_scopes(route) == ("alerts:read",)


def test_competitor_prices_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/variants/{variant_id}/competitor-prices", "GET")
    assert _required_scopes(route) == ("alerts:read",)
