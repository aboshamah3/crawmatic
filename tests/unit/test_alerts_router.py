"""Alerts/price-comparison router unit tests (SPEC-09 T022/T028, US1+US2,
contracts/api-alerts.md).

`GET /v1/variants/{variant_id}/price-comparison` — exercised via
`TestClient` with `app.dependency_overrides[get_current_principal]`
swapped for a fake, DB-less principal bound to the shared
`FakeOrmSession` (`tests/unit/_jobs_fake_session.py`, reused verbatim —
this route only issues `Select` statements). Per the contract: 200
`PriceComparisonResponse` shape from a seeded `variant_price_states`
row; an unknown/cross-workspace variant -> 404; a variant with no price
state yet -> 404 ("no comparison computed yet"); the route declares
`require_scopes("alerts:read")` and a missing scope -> 403.

US2 (T028) adds `GET /v1/alerts/current` (+`/{variant_id}`) and
`GET /v1/alert-events` — the two paginated list routes use
`FakeAlertsListSession` (`tests/unit/_alerts_list_fake_session.py`),
a purpose-built fake supporting `.order_by()`/`.limit()`/the
`tuple_(...) > tuple_(...)` keyset predicate that `FakeOrmSession`
does not (no prior SPEC unit-tests a cursor-paginated list route, so
there was no existing fake to reuse for that shape); the single-alert
route reuses plain `FakeOrmSession` (an equality-only lookup, same
shape as the price-comparison route).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app_shared.enums import AlertEventType, AlertSeverity, AlertStatus, AlertType
from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState
from app_shared.models.catalog import ProductVariant

from app.deps import Principal, get_current_principal
from app.main import app

from unit._alerts_list_fake_session import FakeAlertsListSession
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


# --- US2 (T028): GET /v1/alerts/current (+/{variant_id}), /v1/alert-events --


@pytest.fixture()
def list_fake_session() -> FakeAlertsListSession:
    return FakeAlertsListSession()


def _make_alert_state(
    *,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    variant_id: uuid.UUID | None = None,
    alert_type: AlertType = AlertType.HIGH_PRICE,
    severity: AlertSeverity = AlertSeverity.HIGH,
    status: AlertStatus = AlertStatus.ACTIVE,
    created_at: datetime | None = None,
) -> VariantAlertState:
    now = created_at or datetime.now(timezone.utc)
    state = VariantAlertState(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id or uuid.uuid4(),
        type=alert_type,
        severity=severity,
        status=status,
        client_price=Decimal("100.0000"),
        benchmark_price=Decimal("95.0000"),
        cheapest_competitor_price=Decimal("90.0000"),
        average_competitor_price=Decimal("95.0000"),
        message="m",
        details=None,
        first_seen_at=now,
        last_seen_at=now,
        resolved_at=None,
        created_at=now,
        updated_at=now,
    )
    state.id = uuid.uuid4()
    return state


def _make_alert_event(
    *,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    variant_id: uuid.UUID | None = None,
    alert_state_id: uuid.UUID | None = None,
    event_type: AlertEventType = AlertEventType.CREATED,
    created_at: datetime | None = None,
) -> PriceAlertEvent:
    now = created_at or datetime.now(timezone.utc)
    event = PriceAlertEvent(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id or uuid.uuid4(),
        alert_state_id=alert_state_id or uuid.uuid4(),
        event_type=event_type,
        previous_type=None,
        new_type=AlertType.HIGH_PRICE,
        previous_severity=None,
        new_severity=AlertSeverity.HIGH,
        message="m",
        details=None,
        created_at=now,
    )
    event.id = uuid.uuid4()
    return event


def test_list_current_alerts_returns_200_shape(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    state = _make_alert_state()
    list_fake_session.seed(state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alerts/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["product_variant_id"] == str(state.product_variant_id)
    assert item["type"] == "HIGH_PRICE"
    assert item["severity"] == "HIGH"
    assert item["status"] == "ACTIVE"


def test_list_current_alerts_filters_by_type_and_severity(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    high = _make_alert_state(alert_type=AlertType.HIGH_PRICE, severity=AlertSeverity.HIGH)
    risk = _make_alert_state(alert_type=AlertType.RISK, severity=AlertSeverity.CRITICAL)
    list_fake_session.seed(high, risk)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alerts/current", params={"type": "RISK"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "RISK"

    resp = client.get("/v1/alerts/current", params={"severity": "HIGH"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["severity"] == "HIGH"


def test_list_current_alerts_invalid_type_is_422(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alerts/current", params={"type": "NOT_A_TYPE"})

    assert resp.status_code == 422


def test_list_current_alerts_paginates_deterministically(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    base = datetime.now(timezone.utc)
    states = [
        _make_alert_state(created_at=base + timedelta(seconds=i)) for i in range(3)
    ]
    list_fake_session.seed(*states)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alerts/current", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    assert [item["product_variant_id"] for item in body["items"]] == [
        str(states[0].product_variant_id),
        str(states[1].product_variant_id),
    ]

    resp2 = client.get(
        "/v1/alerts/current", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None
    assert body2["items"][0]["product_variant_id"] == str(states[2].product_variant_id)


def test_list_current_alerts_malformed_cursor_is_422(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alerts/current", params={"cursor": "not-a-valid-cursor!!"})

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


def test_list_current_alerts_missing_scope_is_403(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=[]
    )

    resp = client.get("/v1/alerts/current")

    assert resp.status_code == 403


def test_get_current_alert_returns_200_one(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    state = _make_alert_state()
    fake_session.seed(state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/alerts/current/{state.product_variant_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_variant_id"] == str(state.product_variant_id)
    assert body["type"] == "HIGH_PRICE"


def test_get_current_alert_none_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"]
    )

    resp = client.get(f"/v1/alerts/current/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_get_current_alert_cross_workspace_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    state = _make_alert_state(workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["alerts:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get(f"/v1/alerts/current/{state.product_variant_id}")

    assert resp.status_code == 404


def test_get_current_alert_missing_scope_is_403(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    state = _make_alert_state()
    fake_session.seed(state)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=[]
    )

    resp = client.get(f"/v1/alerts/current/{state.product_variant_id}")

    assert resp.status_code == 403


def test_list_alert_events_returns_200_shape(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    event = _make_alert_event()
    list_fake_session.seed(event)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alert-events")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(event.id)
    assert item["product_variant_id"] == str(event.product_variant_id)
    assert item["event_type"] == "CREATED"


def test_list_alert_events_filters_by_variant(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    target_variant_id = uuid.uuid4()
    matching = _make_alert_event(variant_id=target_variant_id)
    other = _make_alert_event()
    list_fake_session.seed(matching, other)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alert-events", params={"variant_id": str(target_variant_id)})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["product_variant_id"] == str(target_variant_id)


def test_list_alert_events_paginates(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    base = datetime.now(timezone.utc)
    events = [_make_alert_event(created_at=base + timedelta(seconds=i)) for i in range(3)]
    list_fake_session.seed(*events)
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=["alerts:read"]
    )

    resp = client.get("/v1/alert-events", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    resp2 = client.get(
        "/v1/alert-events", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None


def test_list_alert_events_missing_scope_is_403(
    client: TestClient, list_fake_session: FakeAlertsListSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        list_fake_session, scopes=[]
    )

    resp = client.get("/v1/alert-events")

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


def test_list_current_alerts_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/alerts/current", "GET")
    assert _required_scopes(route) == ("alerts:read",)


def test_get_current_alert_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/alerts/current/{variant_id}", "GET")
    assert _required_scopes(route) == ("alerts:read",)


def test_list_alert_events_route_declares_alerts_read_scope() -> None:
    route = _route("/v1/alert-events", "GET")
    assert _required_scopes(route) == ("alerts:read",)
