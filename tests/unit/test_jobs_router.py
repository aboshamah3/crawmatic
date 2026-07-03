"""Jobs router unit tests (SPEC-08 T031, US1, FR-006/008/009, SC-006).

`apps/api/app/routers/jobs.py` — exercised via `TestClient` with
`app.dependency_overrides[get_current_principal]` swapped for a fake,
DB-less principal bound to the shared `FakeOrmSession`
(`tests/unit/_jobs_fake_session.py`), and a patched
`app_shared.jobs.service.enqueue` (no real DB/Redis/Celery broker). Per
`contracts/api-jobs.md`: run-match -> 202 + one job/target scoped,
`MANUAL`/`API`/`requested_by`, enqueue called once; unknown/cross-ws
match -> 404, no job created, no enqueue; `GET /jobs/{id}` /
`GET /jobs/{id}/results` -> 200 shapes, missing job -> 404; every route
declares the correct `require_scopes` (write for run-match, read for
get/results).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app_shared.jobs.service as service_module
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

from app.deps import Principal, get_current_principal
from app.main import app

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()
OTHER_WORKSPACE_ID = uuid.uuid4()


# --- shared plumbing ---------------------------------------------------------


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


def _make_match(*, workspace_id: uuid.UUID = WORKSPACE_ID) -> CompetitorProductMatch:
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    return match


def _make_job(*, workspace_id: uuid.UUID = WORKSPACE_ID) -> ScrapeJob:
    now = datetime.now(timezone.utc)
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=ScrapeJobStatus.PENDING,
        priority=MatchPriority.NORMAL,
        total_targets=1,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        requested_by=uuid.uuid4(),
        source=ScrapeJobSource.API,
        started_at=None,
        completed_at=None,
        created_at=now,
    )
    job.id = uuid.uuid4()
    return job


def _make_target(job: ScrapeJob, *, workspace_id: uuid.UUID = WORKSPACE_ID) -> ScrapeJobTarget:
    now = datetime.now(timezone.utc)
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=ScrapeTargetStatus.PENDING,
        locked_at=None,
        started_at=None,
        completed_at=None,
        error_code=None,
        created_at=now,
    )
    target.id = uuid.uuid4()
    return target


# --- POST /v1/jobs/run/match/{id} --------------------------------------------


def test_run_match_returns_202_with_one_job_and_one_target(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    match = _make_match()
    fake_session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/jobs/run/match/{match.id}")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING"
    job_id = uuid.UUID(body["id"])

    jobs = fake_session._rows.get(ScrapeJob, [])
    targets = fake_session._rows.get(ScrapeJobTarget, [])
    assert len(jobs) == 1
    assert len(targets) == 1

    job = jobs[0]
    assert job.id == job_id
    assert job.type == ScrapeJobType.MANUAL
    assert job.source == ScrapeJobSource.API
    assert job.scope == ScrapeScope.MATCH
    assert job.match_id == match.id
    assert job.requested_by is not None

    assert targets[0].match_id == match.id
    assert targets[0].scrape_job_id == job_id

    assert len(fake_enqueue.calls) == 1
    call = fake_enqueue.calls[0]
    assert call["name"] == SCRAPE_DISPATCH_JOB
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"] == {
        "scrape_job_id": str(job_id),
        "workspace_id": str(WORKSPACE_ID),
    }


def test_run_match_unknown_match_is_404_no_job_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/jobs/run/match/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


def test_run_match_cross_workspace_match_is_404_no_job_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    match = _make_match(workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"], workspace_id=WORKSPACE_ID
    )

    resp = client.post(f"/v1/jobs/run/match/{match.id}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


# --- GET /v1/jobs/{id} --------------------------------------------------------


def test_get_job_returns_200_job_response_shape(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    job = _make_job()
    fake_session.seed(job)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:read"]
    )

    resp = client.get(f"/v1/jobs/{job.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(job.id)
    assert body["status"] == "PENDING"
    assert body["type"] == "MANUAL"
    assert body["scope"] == "MATCH"
    assert body["total_targets"] == 1
    assert body["success_count"] == 0
    assert body["failure_count"] == 0
    assert body["skipped_count"] == 0
    assert body["source"] == "API"


def test_get_job_missing_is_404(client: TestClient, fake_session: FakeOrmSession) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:read"]
    )

    resp = client.get(f"/v1/jobs/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_get_job_cross_workspace_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    job = _make_job(workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(job)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:read"], workspace_id=WORKSPACE_ID
    )

    resp = client.get(f"/v1/jobs/{job.id}")

    assert resp.status_code == 404


# --- GET /v1/jobs/{id}/results ------------------------------------------------


def test_get_job_results_returns_200_results_shape(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    job = _make_job()
    target = _make_target(job)
    fake_session.seed(job, target)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:read"]
    )

    resp = client.get(f"/v1/jobs/{job.id}/results")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(target.id)
    assert item["match_id"] == str(target.match_id)
    assert item["status"] == "PENDING"
    assert item["error_code"] is None


def test_get_job_results_missing_job_is_404(
    client: TestClient, fake_session: FakeOrmSession
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:read"]
    )

    resp = client.get(f"/v1/jobs/{uuid.uuid4()}/results")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


# --- static: declared require_scopes per route -------------------------------


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


_EXPECTED_STATIC_SCOPES: list[tuple[str, str, tuple[str, ...]]] = [
    ("POST", "/v1/jobs/run/match/{match_id}", ("jobs:write",)),
    ("GET", "/v1/jobs/{job_id}", ("jobs:read",)),
    ("GET", "/v1/jobs/{job_id}/results", ("jobs:read",)),
]


@pytest.mark.parametrize("method,path,expected", _EXPECTED_STATIC_SCOPES)
def test_route_declares_expected_require_scopes(
    method: str, path: str, expected: tuple[str, ...]
) -> None:
    route = _route(path, method)
    assert _required_scopes(route) == expected


def test_every_jobs_route_declares_some_require_scopes() -> None:
    for method, path, _expected in _EXPECTED_STATIC_SCOPES:
        route = _route(path, method)
        assert _required_scopes(route) is not None, f"{method} {path} has no require_scopes"
