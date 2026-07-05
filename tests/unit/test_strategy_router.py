"""Strategy discovery-run router unit tests (SPEC-12 US3 T028,
`contracts/discovery.md`, `contracts/api-and-observability.md`, FR-016/
FR-019, US3 AS2).

`apps/api/app/routers/strategy.py` — exercised via `TestClient` with
`app.dependency_overrides[get_current_principal]` swapped for a fake,
DB-less principal bound to the shared `FakeOrmSession`
(`tests/unit/_jobs_fake_session.py`), and a patched
`app_shared.messaging.enqueue` (no real DB/Redis/Celery broker) — same
harness as `tests/unit/test_jobs_router.py`.

Covers: `sample_urls` of length 2/11 -> `422`, no run created, no
enqueue (US3 AS2); a valid 3-10 sample -> `202`, one `PENDING` run
persisted, exactly one `STRATEGY_DISCOVERY_RUN` enqueue on the
`strategy_discovery` queue carrying `run_id`; unknown/cross-workspace
`competitor_id` -> `404`, no run, no enqueue; list/get round-trip.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.strategy as service_module
from app_shared.enums import DiscoveryRunStatus
from app_shared.models.competitors_matches import Competitor
from app_shared.models.strategy import StrategyDiscoveryRun
from app_shared.task_names import STRATEGY_DISCOVERY_RUN

from app.deps import Principal, get_current_principal
from app.main import app

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()
OTHER_WORKSPACE_ID = uuid.uuid4()


class _FakeEnqueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


@pytest.fixture()
def fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> _FakeEnqueue:
    fake = _FakeEnqueue()
    monkeypatch.setattr(service_module, "enqueue", fake)
    return fake


@pytest.fixture()
def fake_session() -> FakeOrmSession:
    return FakeOrmSession()


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


def _make_competitor(*, workspace_id: uuid.UUID = WORKSPACE_ID) -> Competitor:
    competitor = Competitor(workspace_id=workspace_id, name="Acme", domain="acme.example")
    competitor.id = uuid.uuid4()
    return competitor


def _valid_payload(competitor_id: uuid.UUID, *, n_urls: int = 5) -> dict[str, Any]:
    return {
        "competitor_id": str(competitor_id),
        "domain": "acme.example",
        "url_pattern": "acme.example/products/*",
        "sample_urls": [f"https://acme.example/p/{i}" for i in range(n_urls)],
    }


# --- POST /v1/strategy/discovery-runs — sample-bound validation (AS2) -----


@pytest.mark.parametrize("n_urls", [0, 1, 2, 11, 12])
def test_out_of_bounds_sample_size_is_422_no_run_no_enqueue(
    client: TestClient,
    fake_session: FakeOrmSession,
    fake_enqueue: _FakeEnqueue,
    n_urls: int,
) -> None:
    competitor = _make_competitor()
    fake_session.seed(competitor)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:write"]
    )

    resp = client.post(
        "/v1/strategy/discovery-runs", json=_valid_payload(competitor.id, n_urls=n_urls)
    )

    assert resp.status_code == 422
    assert fake_session._rows.get(StrategyDiscoveryRun, []) == []
    assert fake_enqueue.calls == []


@pytest.mark.parametrize("n_urls", [3, 5, 10])
def test_in_bounds_sample_size_is_accepted(
    client: TestClient,
    fake_session: FakeOrmSession,
    fake_enqueue: _FakeEnqueue,
    n_urls: int,
) -> None:
    competitor = _make_competitor()
    fake_session.seed(competitor)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:write"]
    )

    resp = client.post(
        "/v1/strategy/discovery-runs", json=_valid_payload(competitor.id, n_urls=n_urls)
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["sample_size"] == n_urls


# --- POST /v1/strategy/discovery-runs — happy path + enqueue --------------


def test_valid_request_creates_pending_run_and_enqueues_once(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    competitor = _make_competitor()
    fake_session.seed(competitor)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:write"]
    )

    resp = client.post("/v1/strategy/discovery-runs", json=_valid_payload(competitor.id))

    assert resp.status_code == 202
    body = resp.json()
    run_id = uuid.UUID(body["id"])
    assert body["status"] == "PENDING"
    assert body["competitor_id"] == str(competitor.id)
    assert body["sample_size"] == 5
    assert body["winning_access_method"] is None

    runs = fake_session._rows.get(StrategyDiscoveryRun, [])
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status == DiscoveryRunStatus.PENDING

    assert len(fake_enqueue.calls) == 1
    call = fake_enqueue.calls[0]
    assert call["name"] == STRATEGY_DISCOVERY_RUN
    assert call["queue"] == "strategy_discovery"
    assert call["kwargs"]["run_id"] == str(run_id)
    assert call["kwargs"]["workspace_id"] == str(WORKSPACE_ID)
    assert call["kwargs"]["competitor_id"] == str(competitor.id)
    assert call["kwargs"]["triggered_by"] == "OPERATOR"
    assert len(call["kwargs"]["sample_urls"]) == 5


def test_unknown_competitor_is_404_no_run_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:write"]
    )

    resp = client.post("/v1/strategy/discovery-runs", json=_valid_payload(uuid.uuid4()))

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert fake_session._rows.get(StrategyDiscoveryRun, []) == []
    assert fake_enqueue.calls == []


def test_cross_workspace_competitor_is_404_no_run_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    competitor = _make_competitor(workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(competitor)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:write"], workspace_id=WORKSPACE_ID
    )

    resp = client.post("/v1/strategy/discovery-runs", json=_valid_payload(competitor.id))

    assert resp.status_code == 404
    assert fake_session._rows.get(StrategyDiscoveryRun, []) == []
    assert fake_enqueue.calls == []


# --- GET /v1/strategy/discovery-runs[/{id}] --------------------------------


def test_get_discovery_run_returns_200(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    run = StrategyDiscoveryRun(
        workspace_id=WORKSPACE_ID,
        competitor_id=uuid.uuid4(),
        domain="acme.example",
        url_pattern="acme.example/products/*",
        sample_size=5,
        status=DiscoveryRunStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    run.id = uuid.uuid4()
    fake_session.seed(run)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:read"]
    )

    resp = client.get(f"/v1/strategy/discovery-runs/{run.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(run.id)
    assert body["status"] == "PENDING"
    assert body["sample_size"] == 5


def test_get_discovery_run_missing_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["strategy:read"]
    )

    resp = client.get(f"/v1/strategy/discovery-runs/{uuid.uuid4()}")

    assert resp.status_code == 404
