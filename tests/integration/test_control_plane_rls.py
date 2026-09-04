"""Live RLS isolation for `control_plane_rules` (EPA C2, 2026-09-03).

The unit suite (`tests/unit/test_control_plane_routes.py`) proves the
APPLICATION layer scopes every control-plane statement by
`workspace_id`. This file proves the SECOND layer — the one that has to
hold when the first has a bug: the `control_plane_rules_workspace_isolation`
policy created by `alembic/versions/c8d2e3f4a5b6_control_plane.py`, driven
through the same `app.workspace_id` setting the runtime session sets.

Two independent claims, in the two-layer order
`tests/integration/test_rls_cross_workspace.py` established:

1. **App layer.** A rule created for workspace A is absent from
   workspace B's `GET .../control-plane/state`, and the two workspaces'
   identically-named rules (`external_id` is unique only WITHIN a
   tenant) do not collide.
2. **DB layer.** A deliberately UNSCOPED `SELECT * FROM
   control_plane_rules` — no `WHERE workspace_id = ...` at all — returns
   only workspace A's row while `app.workspace_id` names A, and returns
   nothing at all when no workspace context is set (fail closed).

Marked `integration` (deselected by `-m "not integration"`) AND gated on
a reachable `DATABASE_URL`/`AUTH_DATABASE_URL` with the C1 migration
applied, so it skips cleanly — never fails — on a box with no stack.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _app_database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _auth_database_url() -> str | None:
    return os.environ.get("AUTH_DATABASE_URL")


def _reachable_with_control_plane() -> bool:
    """True only if both URLs connect AND `control_plane_rules` exists."""
    urls = (_app_database_url(), _auth_database_url())
    if not all(urls):
        return False
    try:
        from sqlalchemy import inspect

        for url in urls:
            engine = create_engine(url)
            with engine.connect():
                pass
            engine.dispose()

        engine = create_engine(_app_database_url())
        table_names = set(inspect(engine).get_table_names())
        engine.dispose()
        return "control_plane_rules" in table_names
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _reachable_with_control_plane(),
        reason=(
            "Needs a reachable DATABASE_URL (app role, RLS enforced) + "
            "AUTH_DATABASE_URL (BYPASSRLS) with the EPA C1 control-plane "
            "migration applied -- not available in this environment."
        ),
    ),
]

_SERVICE_HEADERS = {"Authorization": "Bearer " + (os.environ.get("SAAS_SERVICE_TOKEN") or "")}


@pytest.fixture()
def auth_engine() -> Iterator[Engine]:
    engine = create_engine(_auth_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_app_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def two_workspaces(auth_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two workspaces, each with one `control_plane_rules` row sharing the
    SAME `external_id` — the unique key is `(workspace_id, external_id)`,
    so a leak would be visible as a collision, not merely as an extra row.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()
    external_id = f"monitor-{uuid.uuid4()}"

    with auth_engine.begin() as conn:
        for workspace_id, name in ((workspace_a, "cp-rls-a"), (workspace_b, "cp-rls-b")):
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, slug, status, created_at, "
                    "updated_at) VALUES (:id, :name, :slug, 'active', now(), now())"
                ),
                {"id": workspace_id, "name": name, "slug": f"{name}-{workspace_id}"},
            )
            conn.execute(
                text(
                    "INSERT INTO control_plane_rules (id, workspace_id, external_id, "
                    "kind, cadence, enabled, target_external_ids, owner_tag, "
                    "quarantined, created_at, updated_at) VALUES (:id, :ws, :eid, "
                    "'MONITOR', 'DAILY', true, '[]'::jsonb, :tag, false, now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "ws": workspace_id,
                    "eid": external_id,
                    "tag": f"crawmatic-saas:{workspace_id}",
                },
            )

    try:
        yield workspace_a, workspace_b
    finally:
        with auth_engine.begin() as conn:
            for workspace_id in (workspace_a, workspace_b):
                conn.execute(
                    text("DELETE FROM control_plane_rules WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                )
                conn.execute(
                    text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id}
                )


def test_an_unscoped_select_sees_only_the_context_workspaces_rule(
    app_engine: Engine, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """No `WHERE workspace_id = ...` at all — RLS alone must isolate."""
    workspace_a, workspace_b = two_workspaces

    with app_engine.connect() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :ws, false)"),
            {"ws": str(workspace_a)},
        )
        rows = conn.execute(
            text("SELECT workspace_id FROM control_plane_rules")  # noqa: workspace-scope
        ).all()

    seen = {row[0] for row in rows}
    assert workspace_a in seen
    assert workspace_b not in seen


def test_no_workspace_context_sees_no_rules_at_all(
    app_engine: Engine, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Fail closed: with no `app.workspace_id` set, neither row is visible."""
    workspace_a, workspace_b = two_workspaces

    with app_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT workspace_id FROM control_plane_rules "  # noqa: workspace-scope
                "WHERE workspace_id IN (:a, :b)"
            ),
            {"a": str(workspace_a), "b": str(workspace_b)},
        ).all()

    assert rows == []


def test_state_route_does_not_leak_the_other_workspaces_rule(
    two_workspaces: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The application layer, on the real routes: each workspace's
    `/state` lists exactly one rule, and it is its own."""
    if not os.environ.get("SAAS_SERVICE_TOKEN"):
        pytest.skip("Needs SAAS_SERVICE_TOKEN to call the service-token surface.")

    from fastapi.testclient import TestClient

    from app.main import app

    workspace_a, workspace_b = two_workspaces
    client = TestClient(app)

    state_a = client.get(
        f"/v1/admin/workspaces/{workspace_a}/control-plane/state",
        headers=_SERVICE_HEADERS,
    )
    state_b = client.get(
        f"/v1/admin/workspaces/{workspace_b}/control-plane/state",
        headers=_SERVICE_HEADERS,
    )
    assert state_a.status_code == 200
    assert state_b.status_code == 200

    owners_a = {rule["owner_tag"] for rule in state_a.json()["rules"]}
    owners_b = {rule["owner_tag"] for rule in state_b.json()["rules"]}
    assert owners_a == {f"crawmatic-saas:{workspace_a}"}
    assert owners_b == {f"crawmatic-saas:{workspace_b}"}
