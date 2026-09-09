"""`GET /admin/scorecard` and `POST /admin/ops/backup-report` — EPA D5
(deep dive §12 item 9), same router and same `require_service_token`
guard as `GET /admin/alerts/active` (`tests/unit/test_admin_ops_endpoint.py`
is this file's template).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app_shared.maintenance.scorecard import BACKUP_REPORT_SCHEMA, ScorecardRow

from app.main import app
from app.routers import admin_ops

TOKEN = "ops-service-token-for-tests"  # noqa: S105 - fixture value, not a credential


class _FakeSession:
    """Never touched directly — every test stubs `read_scorecard_range`."""


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    class _Settings:
        SAAS_SERVICE_TOKEN = TOKEN

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    monkeypatch.setattr(admin_ops, "_get_redis", lambda: None)

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


# --- GET /admin/scorecard ----------------------------------------------------


def test_scorecard_unauthenticated_request_is_rejected(client: TestClient) -> None:
    assert client.get("/admin/scorecard").status_code == 401


def test_scorecard_wrong_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/admin/scorecard", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_scorecard_returns_rows_from_the_read_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        ScorecardRow(date=date(2026, 9, 7), valid_fresh_matches=4, browser_share=0.3),
        ScorecardRow(date=date(2026, 9, 6), valid_fresh_matches=2),
    ]
    captured: dict[str, object] = {}

    def _fake_range(session, since, until):
        captured["since"] = since
        captured["until"] = until
        return rows

    monkeypatch.setattr(admin_ops, "read_scorecard_range", _fake_range)

    resp = client.get("/admin/scorecard?days=30", headers=auth())
    body = resp.json()

    assert resp.status_code == 200
    assert body["days_requested"] == 30
    assert body["days_available"] == 2
    assert body["rows"][0]["date"] == "2026-09-07"
    assert body["rows"][0]["valid_fresh_matches"] == 4
    assert body["rows"][0]["browser_share"] == 0.3
    # `since`/`until` span exactly `days` calendar days, ending today.
    assert (captured["until"] - captured["since"]).days == 29


def test_scorecard_default_days_is_30(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def _fake_range(session, since, until):
        captured["days"] = (until - since).days + 1
        return []

    monkeypatch.setattr(admin_ops, "read_scorecard_range", _fake_range)
    resp = client.get("/admin/scorecard", headers=auth())
    assert resp.status_code == 200
    assert captured["days"] == 30


def test_scorecard_excluded_from_the_public_openapi_document() -> None:
    from app.openapi_public import build_public_openapi

    spec = build_public_openapi(app)
    assert not any(path.startswith("/admin/scorecard") for path in spec.get("paths", {}))


# --- POST /admin/ops/backup-report -------------------------------------------


VALID_PAYLOAD = {
    "schema": BACKUP_REPORT_SCHEMA,
    "backup_set": "set-20260907T080000Z",
    "stage": "backup",
    "source": "dr-backup",
    "created_utc": "2026-09-07T08:00:00Z",
    "private_network": True,
    "targets": [],
    "totals": {"bytes_exported": 0, "bytes_encrypted": 0, "bytes_on_wire": 0, "seconds": 0},
    "retention": {},
}


def test_backup_report_unauthenticated_request_is_rejected(client: TestClient) -> None:
    assert client.post("/admin/ops/backup-report", json=VALID_PAYLOAD).status_code == 401


def test_backup_report_accepts_a_recognised_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded_calls: list[tuple] = []
    monkeypatch.setattr(
        admin_ops,
        "record_backup_report",
        lambda redis_client, payload: recorded_calls.append((redis_client, payload)) or True,
    )

    resp = client.post("/admin/ops/backup-report", json=VALID_PAYLOAD, headers=auth())
    body = resp.json()

    assert resp.status_code == 202
    assert body["recorded"] is True
    assert body["backup_set"] == "set-20260907T080000Z"
    assert len(recorded_calls) == 1


def test_backup_report_rejects_an_unrecognised_schema(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_ops, "record_backup_report", lambda *a, **k: True)
    bad = dict(VALID_PAYLOAD, schema="some.other.v2")
    resp = client.post("/admin/ops/backup-report", json=bad, headers=auth())
    assert resp.status_code == 422


def test_backup_report_missing_schema_field_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_ops, "record_backup_report", lambda *a, **k: True)
    bad = {k: v for k, v in VALID_PAYLOAD.items() if k != "schema"}
    resp = client.post("/admin/ops/backup-report", json=bad, headers=auth())
    assert resp.status_code == 422


def test_backup_report_still_returns_202_when_redis_recording_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: the sender (`dr_post_backup_report`) treats a non-2xx
    as "kept locally, WARN" -- so a Redis outage on this end must not
    turn into a POST failure the sender would then retry forever."""
    monkeypatch.setattr(admin_ops, "record_backup_report", lambda *a, **k: False)
    resp = client.post("/admin/ops/backup-report", json=VALID_PAYLOAD, headers=auth())
    assert resp.status_code == 202
    assert resp.json()["recorded"] is False


def test_backup_report_excluded_from_the_public_openapi_document() -> None:
    from app.openapi_public import build_public_openapi

    spec = build_public_openapi(app)
    assert not any(
        path.startswith("/admin/ops/backup-report") for path in spec.get("paths", {})
    )
