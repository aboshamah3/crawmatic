"""`POST /v1/admin/jobs/{job_id}/cancel` router tests (EPA A2, 2026-08-25).

`apps/api/app/routers/jobs_admin.py` — exercised via `TestClient` with
`app.dependency_overrides[get_current_principal]` swapped for a fake,
DB-less principal bound to the cancellation session double (the same
pattern as `tests/unit/test_jobs_router.py`), so there is no real
Postgres, Redis or broker anywhere in this module.

What is pinned here:

1. **The scope gate.** A principal without `jobs:cancel` gets a 403 and
   the job is left completely untouched — no target moved, no event
   recorded. This is the test that matters most: cancellation is
   irreversible, so "the guard is declared" is not enough, the guard has
   to actually stop the write.
2. `jobs:write` alone is NOT enough. `jobs:cancel` is a separate,
   separately grantable capability precisely so a key that can start
   jobs cannot close them (see `Scope.JOBS_CANCEL`).
3. **The actor comes from the principal, not the request.** The body has
   no actor field at all, and what lands on the target rows is the
   authenticated identity.
4. The happy path returns the `CancellationReport` shape, and a replay
   answers `targets_cancelled=0`/`idempotent_replay=true`.
5. A missing/cross-workspace job is a 404 — the same answer for both, so
   the endpoint is not a cross-tenant existence oracle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app_shared.enums import ScrapeJobStatus, ScrapeTargetStatus

from app.deps import Principal, get_current_principal
from app.main import app

# The seeding helpers and the session double live with the unit tests for
# the function under the route (`test_cancellation.py`) — reusing them is
# what keeps "what the route does" and "what the function does" describing
# the same fixtures rather than two subtly different worlds.
from unit.jobs.test_cancellation import (
    REASON,
    _CancellationSession,
    _log_out_of_band_steps,
    _seeded,
)

CANCEL_SCOPE = "jobs:cancel"


def _override_principal(session: Any, *, scopes: list[str], workspace_id: uuid.UUID):
    principal_id = uuid.uuid4()

    def _dependency() -> Iterator[tuple[Any, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=principal_id,
            role=None,
            scopes=scopes,
            workspace_id=workspace_id,
        )

    _dependency.principal_id = principal_id  # type: ignore[attr-defined]
    return _dependency


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_current_principal, None)


def _install(session: _CancellationSession, scopes: list[str], workspace_id: uuid.UUID):
    override = _override_principal(session, scopes=scopes, workspace_id=workspace_id)
    app.dependency_overrides[get_current_principal] = override
    return override


def _redis_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 3 of the ordering protocol talks to Redis; stub it out.

    Its failure is already swallowed by design, but letting the call
    attempt a real connection would make these tests depend on a Redis
    being reachable (and slow when it is not).
    """
    import app_shared.jobs.cancellation as cancellation_module

    monkeypatch.setattr(cancellation_module, "_delete_dispatch_guards", lambda *_a: None)


# --- 1/2: the scope gate ----------------------------------------------------


@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param([], id="no-scopes"),
        pytest.param(["jobs:read"], id="read-only"),
        pytest.param(["jobs:write", "jobs:run"], id="jobs-write-is-not-enough"),
    ],
)
def test_cancel_without_the_scope_is_403_and_changes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, scopes: list[str]
) -> None:
    _redis_free(monkeypatch)
    session, job, targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.STARTED,
    )
    _install(session, scopes, job.workspace_id)

    response = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})

    assert response.status_code == 403

    # The refusal is not merely a status code: nothing moved.
    assert job.status is ScrapeJobStatus.RUNNING
    assert job.cancellation_generation == 0
    assert all(target.status is not ScrapeTargetStatus.CANCELLED for target in targets)
    assert all(target.cancelled_by is None for target in targets)
    assert session.inserts == []


# --- 3/4: the happy path ----------------------------------------------------


def test_cancel_with_the_scope_terminalizes_and_reports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redis_free(monkeypatch)
    session, job, targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.STARTED,
        ScrapeTargetStatus.COMPLETED,
    )
    override = _install(session, [CANCEL_SCOPE], job.workspace_id)

    response = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == str(job.id)
    assert body["targets_cancelled"] == 2
    assert body["targets_already_terminal"] == 1
    assert body["idempotent_replay"] is False
    assert body["outbox_message_id"] is not None

    assert job.status is ScrapeJobStatus.CANCELLED
    assert job.cancellation_generation == 1

    # The actor is the authenticated principal, not anything the caller
    # could type into the body.
    expected_actor = f"api_key:{override.principal_id}"  # type: ignore[attr-defined]
    assert body["actor"] == expected_actor
    cancelled = [t for t in targets if t.status is ScrapeTargetStatus.CANCELLED]
    assert len(cancelled) == 2
    assert {t.cancelled_by for t in cancelled} == {expected_actor}
    assert {t.cancelled_reason for t in cancelled} == {REASON}


def test_cancel_body_has_no_actor_field_to_forge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplied `actor`/`cancelled_by` is ignored, never honoured."""
    _redis_free(monkeypatch)
    session, job, targets = _seeded(ScrapeTargetStatus.PENDING)
    override = _install(session, [CANCEL_SCOPE], job.workspace_id)

    response = client.post(
        f"/v1/admin/jobs/{job.id}/cancel",
        json={"reason": REASON, "actor": "someone-else", "cancelled_by": "someone-else"},
    )

    assert response.status_code == 200
    expected_actor = f"api_key:{override.principal_id}"  # type: ignore[attr-defined]
    assert response.json()["actor"] == expected_actor
    assert targets[0].cancelled_by == expected_actor


def test_cancel_is_idempotent_over_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redis_free(monkeypatch)
    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING, ScrapeTargetStatus.PENDING)
    _install(session, [CANCEL_SCOPE], job.workspace_id)

    first = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})
    second = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["targets_cancelled"] == 2
    assert second.json()["targets_cancelled"] == 0
    assert second.json()["idempotent_replay"] is True
    assert second.json()["outbox_message_id"] is None
    # Exactly one durable event across both calls.
    assert len(session.inserts) == 1
    assert job.cancellation_generation == 1


def test_blank_reason_is_rejected_before_anything_moves(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redis_free(monkeypatch)
    session, job, targets = _seeded(ScrapeTargetStatus.PENDING)
    _install(session, [CANCEL_SCOPE], job.workspace_id)

    response = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": "   "})

    assert response.status_code == 422
    assert job.status is ScrapeJobStatus.RUNNING
    assert targets[0].status is ScrapeTargetStatus.PENDING


# --- the ordering protocol, on the route path -------------------------------


def test_route_commits_the_fence_before_the_out_of_band_cleanup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP path must honour the module's own ordering protocol.

    `cancellation.py` justifies deleting the `dispatched:{job}:*` guards
    with "safe only *because* step 1 already committed" — but this route's
    session is committed by `deps.get_current_principal` only *after* the
    handler returns, i.e. after the cleanup. The fence commit is therefore
    the cancellation function's own responsibility, and this is the test
    that fails if it moves back out (EPA Phase A review F-1).

    Deliberately asserted through `TestClient` rather than by calling the
    function directly: F-1 was not a defect in the function's logic, it
    was a defect in what the *route's* transaction lifecycle did to that
    logic, and only an end-to-end request exercises that.
    """
    import app_shared.jobs.cancellation as cancellation_module

    session, job, _targets = _seeded(
        ScrapeTargetStatus.PENDING, ScrapeTargetStatus.STARTED
    )
    _log_out_of_band_steps(monkeypatch, cancellation_module, session)
    _install(session, [CANCEL_SCOPE], job.workspace_id)

    response = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})

    assert response.status_code == 200
    # The fence is durable before ANY of steps 2-4 is attempted...
    assert session.events == ["commit", "scrapyd", "guards", "reservations"]
    # ...and it is the real fence that was committed, not an empty txn.
    assert session.committed is True
    assert job.status is ScrapeJobStatus.CANCELLED
    assert job.cancellation_generation == 1


# --- 5: unknown / cross-workspace ------------------------------------------


def test_unknown_job_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _redis_free(monkeypatch)
    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING)
    _install(session, [CANCEL_SCOPE], job.workspace_id)

    response = client.post(f"/v1/admin/jobs/{uuid.uuid4()}/cancel", json={"reason": REASON})

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_cancel_never_reaches_beyond_the_resolved_workspace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second workspace's job in the same session is untouched.

    Cross-tenant *invisibility* itself is RLS's job (plus the explicit
    `workspace_id` predicate in `cancellation.py`) and cannot be
    reproduced against an RLS-free session double — that belongs to the
    integration suite. What IS testable here, and what actually matters
    for a destructive endpoint, is that the cancellation's own row
    selection is bounded by the workspace it resolved: given two jobs
    from two workspaces visible in one session, cancelling one moves
    nothing belonging to the other.
    """
    _redis_free(monkeypatch)
    session, job, targets = _seeded(ScrapeTargetStatus.PENDING, ScrapeTargetStatus.PENDING)

    other_session, other_job, other_targets = _seeded(ScrapeTargetStatus.PENDING)
    # Same session, two workspaces' rows side by side.
    session.seed(other_job, *other_targets)

    _install(session, [CANCEL_SCOPE], job.workspace_id)
    response = client.post(f"/v1/admin/jobs/{job.id}/cancel", json={"reason": REASON})

    assert response.status_code == 200
    assert response.json()["targets_cancelled"] == 2
    assert all(t.status is ScrapeTargetStatus.CANCELLED for t in targets)

    assert other_job.status is ScrapeJobStatus.RUNNING
    assert other_job.cancellation_generation == 0
    assert all(t.status is ScrapeTargetStatus.PENDING for t in other_targets)
    assert other_session is not session  # the second fixture's own session is unused


# --- static: the declared scope --------------------------------------------


def _iter_api_routes():
    """Flatten `app.routes`, which holds lazy `_IncludedRouter` wrappers.

    Same helper as `tests/unit/test_jobs_router.py` — this FastAPI version
    does not eagerly flatten included routers into `app.routes`.
    """
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


def test_cancel_route_declares_the_jobs_cancel_scope() -> None:
    routes = [
        route
        for route in _iter_api_routes()
        if isinstance(route, APIRoute)
        and route.path == "/v1/admin/jobs/{job_id}/cancel"
        and "POST" in route.methods
    ]
    assert len(routes) == 1, "the cancel route must be registered exactly once"
    assert _required_scopes(routes[0]) == (CANCEL_SCOPE,)


def test_jobs_cancel_is_in_neither_bootstrap_nor_connector_scopes() -> None:
    """A connector key in a WordPress install can start jobs, never close them."""
    from app.routers.admin import BOOTSTRAP_SCOPES, CONNECTOR_SCOPES

    assert CANCEL_SCOPE not in CONNECTOR_SCOPES
    assert CANCEL_SCOPE not in BOOTSTRAP_SCOPES
