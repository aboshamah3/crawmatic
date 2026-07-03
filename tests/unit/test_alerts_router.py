"""Alerts/price-comparison router unit tests (SPEC-09 T022, US1, contracts/api-alerts.md).

`GET /v1/variants/{variant_id}/price-comparison` — exercised via
`TestClient` with `app.dependency_overrides[get_current_principal]`
swapped for a fake, DB-less principal bound to the shared
`FakeOrmSession` (`tests/unit/_jobs_fake_session.py`, reused verbatim —
this route only issues `Select` statements). Per the contract: 200
`PriceComparisonResponse` shape from a seeded `variant_price_states`
row; an unknown/cross-workspace variant -> 404; a variant with no price
state yet -> 404 ("no comparison computed yet"); the route declares
`require_scopes("alerts:read")` and a missing scope -> 403.
(`alerts/current` + `alert-events` cases land in US2, T028.)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app_shared.enums import AlertSeverity, AlertType
from app_shared.models.alerts import VariantPriceState
from app_shared.models.catalog import ProductVariant

from app.deps import Principal, get_current_principal
from app.main import app

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()
OTHER_WORKSPACE_ID = uuid.uuid4()


def _override_principal(
    session: FakeOrmSession, *, scopes: list[str], workspace_id: uuid.UUID = WORKSPACE_ID
):
    def _dependency() -> Iterator[tuple[FakeOrmSession, Principal]]:
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
def fake_session() -> FakeOrmSession:
    return FakeOrmSession()


def _make_variant(
    *, workspace_id: uuid.UUID = WORKSPACE_ID, product_id: uuid.UUID | None = None
) -> ProductVariant:
    variant = ProductVariant(
        workspace_id=workspace_id,
        product_id=product_id or uuid.uuid4(),
        title="Widget",
        current_price=Decimal("2999.0000"),
        currency="SAR",
        status="active",
    )
    variant.id = uuid.uuid4()
    return variant


def _make_price_state(
    variant: ProductVariant, *, workspace_id: uuid.UUID = WORKSPACE_ID
) -> VariantPriceState:
    now = datetime.now(timezone.utc)
    price_state = VariantPriceState(
        workspace_id=workspace_id,
        product_id=variant.product_id,
        product_variant_id=variant.id,
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
    price_state.id = uuid.uuid4()
    return price_state


# --- GET /v1/variants/{id}/price-comparison ----------------------------------


def test_price_comparison_returns_200_shape(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    variant = _make_variant()
    price_state = _make_price_state(variant)
    fake_session.seed(variant, price_state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/price-comparison")

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_variant_id"] == str(variant.id)
    assert body["currency"] == "SAR"
    assert Decimal(body["client_price"]) == Decimal("2999.0000")
    assert Decimal(body["cheapest_competitor_price"]) == Decimal("2799.0000")
    assert Decimal(body["average_competitor_price"]) == Decimal("2899.0000")
    assert Decimal(body["highest_competitor_price"]) == Decimal("3099.0000")
    assert body["comparable_competitor_count"] == 3
    assert body["alert_type"] == "HIGH_PRICE"
    assert body["alert_severity"] == "HIGH"
    assert body["calculated_at"] is not None


def test_price_comparison_unknown_variant_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{uuid.uuid4()}/price-comparison")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_price_comparison_cross_workspace_variant_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    variant = _make_variant(workspace_id=OTHER_WORKSPACE_ID)
    price_state = _make_price_state(variant, workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(variant, price_state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get(f"/v1/variants/{variant.id}/price-comparison")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_price_comparison_never_analyzed_variant_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    variant = _make_variant()
    fake_session.seed(variant)  # no VariantPriceState seeded.
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/variants/{variant.id}/price-comparison")

    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"]["code"] == "NOT_FOUND"
    assert "no" in body["detail"]["error"]["message"].lower()


def test_price_comparison_missing_scope_is_403(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    variant = _make_variant()
    fake_session.seed(variant, _make_price_state(variant))
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=[]
    )

    resp = client.get(f"/v1/variants/{variant.id}/price-comparison")

    assert resp.status_code == 403


# --- static: declared require_scopes -----------------------------------------


def _iter_api_routes() -> Iterator[APIRoute]:
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        elif isinstance(route, APIRoute):
            yield route


def _required_scopes(route: APIRoute) -> tuple[str, ...] | None:
    for dep in route.dependant.dependencies:
        call = dep.call
        freevars = getattr(call.__code__, "co_freevars", ())
        if "scopes" in freevars and call.__closure__:
            idx = freevars.index("scopes")
            return call.__closure__[idx].cell_contents
    return None


def _route(path: str, method: str) -> APIRoute:
    method = method.upper()
    for route in _iter_api_routes():
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"no route found for {method} {path}")


def test_price_comparison_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/variants/{variant_id}/price-comparison", "GET")
    assert _required_scopes(route) == ("alerts:read",)
