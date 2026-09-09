"""A paired plugin can actually run its live check (review R03).

The finding
-----------
The WooCommerce plugin's "refresh prices now" button posts to
`POST /v1/variants/{id}/rescrape` (`jobs:write`) and then polls
`GET /v1/jobs/{job_id}` (`jobs:read`). `CONNECTOR_SCOPES` — the fixed,
non-caller-suppliable scope set every connector key is minted with —
contained NEITHER, so a correctly paired plugin got 403 on a shipped
feature, 100% of the time. `tests/integration/test_api_contract.py`
recorded that gap on purpose, from both sides, and asked for a
deliberate decision rather than a scope-list edit.

The decision
------------
Grant them. The reason the two scopes were withheld was that a
WordPress-resident credential is the most exposed one in the system, and
`jobs:write` spends scrape budget. That risk is real but it is not
*unbounded*: both endpoints resolve every row through `scoped_get(...,
principal.workspace_id)`, so the key can only spend the budget of the
one workspace it was minted for, and it can only see that workspace's
jobs. `jobs:cancel` stays out — it terminalizes rows nobody can get
back, and no plugin feature needs it.

What this file proves, and why each half is necessary
-----------------------------------------------------
The positive half alone would be a scope-list edit with a test attached.
The negative half is the one that makes the grant defensible: a token
carrying exactly `CONNECTOR_SCOPES`, minted for workspace A, must not be
able to start or poll work in workspace B — and the refusal must be
`404`, this repo's convention for a cross-tenant row (a 403 would
confirm the id exists, which is itself a cross-tenant read).

Driven the same DB-less way as `test_variants_rescrape_route.py`:
`TestClient` over `FakeOrmSession` with `get_current_principal`
overridden, and the entitlement gate stubbed for the same reason that
file stubs it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app_shared.jobs.service as service_module
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
)
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob

from app.deps import Principal, get_current_principal
from app.main import app
import app.routers.variants as variants_router
from app.routers.admin import CONNECTOR_SCOPES

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_A = uuid.uuid4()
WORKSPACE_B = uuid.uuid4()


# --- plumbing (mirrors test_variants_rescrape_route.py) ----------------------


def _outbox_shim(calls: list[dict[str, Any]]):
    def _write(session, *, workspace_id, task_name, queue, kwargs=None, **rest):
        calls.append({"name": task_name, "queue": queue, "kwargs": kwargs})

    return _write


@pytest.fixture()
def outbox_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(service_module, "write_outbox_message", _outbox_shim(calls))
    return calls


@pytest.fixture(autouse=True)
def stub_entitlement_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`assert_workspace_entitled` reads durable evidence this fake
    session has none of. Stubbed here for the same reason (and with the
    same `setattr` fail-loud property) as in the rescrape route's own
    unit tests; its real behaviour is proven against a database in
    `tests/integration/test_cost_authorization.py`."""
    monkeypatch.setattr(variants_router, "assert_workspace_entitled", lambda ws: None)


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _connector_principal(session: FakeOrmSession, *, workspace_id: uuid.UUID):
    """A principal carrying EXACTLY the minted connector scope set.

    Not a hand-written list: the point of these tests is that the shipped
    `CONNECTOR_SCOPES` is sufficient, so narrowing it later must turn
    them red.
    """

    def _dependency() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=list(CONNECTOR_SCOPES),
            workspace_id=workspace_id,
        )

    return _dependency


def _seed_variant_with_match(
    session: FakeOrmSession, *, workspace_id: uuid.UUID
) -> ProductVariant:
    variant = ProductVariant(
        workspace_id=workspace_id, product_id=uuid.uuid4(), title="Variant A"
    )
    variant.id = uuid.uuid4()
    session.seed(variant)
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant.id,
        competitor_id=uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    session.seed(match)
    return variant


def _seed_job(session: FakeOrmSession, *, workspace_id: uuid.UUID) -> ScrapeJob:
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        product_variant_id=uuid.uuid4(),
        status=ScrapeJobStatus.RUNNING,
        priority=MatchPriority.HIGH,
        total_targets=1,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        requested_by=uuid.uuid4(),
        source=ScrapeJobSource.API,
        created_at=datetime.now(timezone.utc),
    )
    job.id = uuid.uuid4()
    session.seed(job)
    return job


# --- the scope set itself ----------------------------------------------------


def test_connector_scopes_carry_both_live_check_scopes() -> None:
    """The grant, stated once.

    Companion to `test_api_contract.py`'s pinning of the two ROUTES'
    required scopes: this pins that the minted key satisfies them.
    """
    granted = {str(scope) for scope in CONNECTOR_SCOPES}

    assert {"jobs:read", "jobs:write"} <= granted


def test_connector_scopes_still_exclude_the_destructive_ones() -> None:
    """Widening for the live check must not widen for anything else.

    `jobs:cancel` terminalizes rows nobody can get back; the
    `webhooks`/`scrape_profiles`/`domain_rules` writes are SaaS-worker
    business that never belongs on a credential sitting on a merchant's
    own server.
    """
    granted = {str(scope) for scope in CONNECTOR_SCOPES}
    forbidden = {
        "jobs:cancel",
        "products:write",
        "variants:write",
        "refresh_rules:write",
        "webhooks:write",
        "scrape_profiles:write",
        "domain_rules:write",
    }

    assert not (forbidden & granted), f"connector key widened too far: {sorted(forbidden & granted)}"


# --- positive: the live check completes --------------------------------------


def test_a_connector_key_can_start_a_rescrape(
    client: TestClient, session: FakeOrmSession, outbox_calls: list[dict[str, Any]]
) -> None:
    """The half of the plugin's button that 403'd before this change."""
    variant = _seed_variant_with_match(session, workspace_id=WORKSPACE_A)
    app.dependency_overrides[get_current_principal] = _connector_principal(
        session, workspace_id=WORKSPACE_A
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202, resp.json()
    assert resp.json()["match_count"] == 1
    assert len(outbox_calls) == 1, "the dispatch was really published"


def test_a_connector_key_can_poll_the_job_it_started(
    client: TestClient, session: FakeOrmSession, outbox_calls: list[dict[str, Any]]
) -> None:
    """Starting a job the key cannot then read would be a worse failure
    than the 403: the plugin would spend budget and never show a price."""
    variant = _seed_variant_with_match(session, workspace_id=WORKSPACE_A)
    app.dependency_overrides[get_current_principal] = _connector_principal(
        session, workspace_id=WORKSPACE_A
    )

    started = client.post(f"/v1/variants/{variant.id}/rescrape")
    assert started.status_code == 202, started.json()
    job_id = started.json()["job_id"]

    polled = client.get(f"/v1/jobs/{job_id}")

    assert polled.status_code == 200, polled.json()
    assert polled.json()["id"] == job_id


# --- negative: the grant is workspace-bounded --------------------------------


def test_a_connector_key_cannot_poll_another_workspaces_job(
    client: TestClient, session: FakeOrmSession
) -> None:
    """404, not 403 — this repo's convention for a cross-tenant row.

    A 403 would confirm the id exists in some workspace, which is itself
    a cross-tenant read. `scoped_get` is what makes the answer 404, and
    it is the reason `jobs:read` on a WordPress-resident key is a bounded
    grant rather than a fleet-wide one.
    """
    foreign_job = _seed_job(session, workspace_id=WORKSPACE_B)
    app.dependency_overrides[get_current_principal] = _connector_principal(
        session, workspace_id=WORKSPACE_A
    )

    resp = client.get(f"/v1/jobs/{foreign_job.id}")

    assert resp.status_code == 404, resp.json()
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_a_connector_key_cannot_rescrape_another_workspaces_variant(
    client: TestClient, session: FakeOrmSession, outbox_calls: list[dict[str, Any]]
) -> None:
    """The budget-spending half of the same boundary: no job, no publish."""
    foreign_variant = _seed_variant_with_match(session, workspace_id=WORKSPACE_B)
    app.dependency_overrides[get_current_principal] = _connector_principal(
        session, workspace_id=WORKSPACE_A
    )

    resp = client.post(f"/v1/variants/{foreign_variant.id}/rescrape")

    assert resp.status_code == 404, resp.json()
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert session._rows.get(ScrapeJob, []) == [], "a cross-tenant 404 created a job"
    assert outbox_calls == [], "a cross-tenant 404 published a dispatch"


def test_a_connector_key_cannot_read_another_workspaces_job_results(
    client: TestClient, session: FakeOrmSession
) -> None:
    """`GET /v1/jobs/{id}/results` is the other `jobs:read` route the
    plugin's poll loop touches; the same boundary has to hold on it."""
    foreign_job = _seed_job(session, workspace_id=WORKSPACE_B)
    app.dependency_overrides[get_current_principal] = _connector_principal(
        session, workspace_id=WORKSPACE_A
    )

    resp = client.get(f"/v1/jobs/{foreign_job.id}/results")

    assert resp.status_code == 404, resp.json()


# --- the mint path records the scopes ---------------------------------------


def test_minted_keys_carry_their_scopes_on_the_row_not_by_reference() -> None:
    """Why widening this list does NOT widen already-issued keys.

    `create_connector_key` writes `scopes=list(CONNECTOR_SCOPES)` onto the
    `api_keys` row, so an existing key keeps the set it was minted with
    and only a re-mint picks the new one up. That is the safe direction
    (no silent privilege growth) and it is also the operational cost of
    this change: every store paired before the widening keeps 403-ing on
    the live check until its key is replaced, mint-then-revoke, driven
    from the SaaS. The three reissue paths and how to spot a stale key
    are in `docs/ops/CONNECTOR_KEY_REISSUE.md`.
    """
    import inspect

    from app.routers import admin as admin_router

    source = inspect.getsource(admin_router.create_connector_key)
    assert "scopes=list(CONNECTOR_SCOPES)" in source, (
        "the mint no longer copies the scope list onto the row; if scopes became a "
        "reference/lookup, every previously issued connector key would silently gain "
        "jobs:write the moment this list changed"
    )
