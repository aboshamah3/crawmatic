"""`GET /admin/alerts/active` — EPA B9 (F22): the poll target for the
existing `crawmatic-ops-alerts` cron.

Same auth posture as `/ops/metrics` (cross-workspace fleet data, so the
same `require_service_token` seam, never the tenant auth seam) — this
file follows `tests/unit/test_ops_metrics_endpoint.py`'s exact pattern:
`collect_snapshot` stubbed out, the router's own DB dependency
overridden with a tiny fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app_shared.opsmetrics.rules import Alert, Category, Severity
from app_shared.opsmetrics.snapshot import OpsSnapshot

from app.main import app
from app.routers import admin_ops

TOKEN = "ops-service-token-for-tests"  # noqa: S105 - fixture value, not a credential
SNAPSHOT = OpsSnapshot(collected_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC))


class _FakeSession:
    """Never used — `collect_snapshot` is stubbed out in these tests."""


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    class _Settings:
        SAAS_SERVICE_TOKEN = TOKEN

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    monkeypatch.setattr(admin_ops, "_get_redis", lambda: None)
    monkeypatch.setattr(admin_ops, "_get_settings_or_none", lambda: None)
    monkeypatch.setattr(admin_ops, "_get_scrapyd_status", lambda *a, **k: None)
    monkeypatch.setattr(admin_ops, "collect_snapshot", lambda *a, **k: SNAPSHOT)

    def _fake_session() -> Iterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[admin_ops._get_ops_session] = _fake_session
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    assert client.get("/admin/alerts/active").status_code == 401


def test_wrong_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/admin/alerts/active", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_authenticated_request_returns_the_firing_alerts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert = Alert(
        rule_id="test.rule",
        severity=Severity.CRITICAL,
        category=Category.RELIABILITY,
        message="synthetic",
        justification="test",
    )
    monkeypatch.setattr(admin_ops, "evaluate", lambda snapshot: [alert])

    resp = client.get("/admin/alerts/active", headers=auth())
    body = resp.json()

    assert resp.status_code == 200
    assert body["alert_count"] == 1
    assert body["worst_severity"] == "CRITICAL"
    assert body["alerts"][0]["rule_id"] == "test.rule"


def test_excluded_from_the_public_openapi_document() -> None:
    from app.openapi_public import build_public_openapi

    spec = build_public_openapi(app)
    assert not any(path.startswith("/admin/alerts") for path in spec.get("paths", {}))
