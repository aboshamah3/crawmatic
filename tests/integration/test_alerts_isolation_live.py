"""Live cross-workspace isolation + scope-gating test across all three
SPEC-09 alerts tables (Isolation/T036, FR-005/FR-020, SC-008) — ⏸ DEFERRED.

Mirrors `tests/integration/test_observations_isolation_live.py` (SPEC-07)
and `tests/integration/test_competitors_matches_isolation_live.py`
(SPEC-05), combined: the app-scoping-helper + raw-RLS probes from the
former (this spec's tables carry the same soft-reference philosophy —
only `workspace_id` gets a real FK), plus the API-level 404/403 checks
from the latter (this spec, unlike SPEC-07's observation tables, DOES
add a `/v1/...` read surface over these three tables).

Proves, on `variant_price_states` / `variant_alert_states` /
`price_alert_events`:

1. `scoped_select` (with `app.workspace_id` set to workspace A) never
   returns workspace B's row, on all three tables.
2. A deliberately app-**unscoped** raw query (no `WHERE workspace_id =
   ...` at all) with `app.workspace_id` set to A still returns 0 of B's
   rows on all three tables — RLS alone enforces isolation.
3. With **no** `app.workspace_id` context set at all, the same raw
   queries return 0 rows for either workspace's seeded row on all three
   tables (fail closed).
4. Workspace A's `alerts:read`-scoped API key: `GET
   /v1/variants/{b_variant_id}/price-comparison` -> 404;
   `GET /v1/alerts/current/{b_variant_id}` -> 404; `GET
   /v1/alerts/current` / `GET /v1/alert-events` never include B's rows.
5. An API key with no `alerts:read` scope -> 403 on every alerts route.

Needs a reachable Postgres (`DATABASE_URL`, SPEC-09 migration applied)
AND a reachable Redis (`REDIS_URL` — `recompute_variant` touches it via
the Celery task import chain even for a direct call). Not runnable in
the no-Docker-daemon build environment used to author this feature —
SKIPS cleanly whenever either isn't reachable or the SPEC-09 alerts
tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host with the SPEC-09 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ._alerts_live_support import (
    alerts_live_reachable,
    cleanup_alerts_rows,
    run_recompute_variant,
    seed_match_current_price,
    set_variant_price,
)
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

pytestmark = pytest.mark.skipif(
    not alerts_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, SPEC-09 migration applied) "
        "and a reachable Redis (REDIS_URL) in this environment."
    ),
)


@dataclass
class _IsolationFixture:
    seeded_a: SeededWorkspace
    seeded_b: SeededWorkspace
    api_key_a_read: str
    api_key_a_no_scope: str


def _seed_analyzed_variant(name_prefix: str) -> SeededWorkspace:
    seeded = seed_workspace_with_variant(name_prefix)
    set_variant_price(seeded.product_variant_id, price=Decimal("100.0000"), currency="USD")
    competitor_id = seed_competitor(seeded, f"{name_prefix} Competitor")
    match_id = seed_match(seeded, competitor_id, f"https://{name_prefix}.invalid/p/1")
    seed_match_current_price(seeded, match_id, competitor_id, price=Decimal("50.0000"))

    result = run_recompute_variant(
        workspace_id=seeded.workspace_id,
        product_variant_id=seeded.product_variant_id,
        product_id=seeded.product_id,
    )
    assert result.returncode == 0, (
        f"recompute_variant subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return seeded


@pytest.fixture()
def fixture() -> Iterator[_IsolationFixture]:
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus
    from app_shared.models import ApiKey
    from app_shared.security.api_keys import generate_api_key

    seeded_a = _seed_analyzed_variant("alerts-isolation-live-a")
    seeded_b = _seed_analyzed_variant("alerts-isolation-live-b")

    with get_session() as session:
        secret_read, prefix_read, hash_read = generate_api_key()
        key_read = ApiKey(
            workspace_id=seeded_a.workspace_id,
            name="alerts-isolation-live-read-key",
            key_prefix=prefix_read,
            key_hash=hash_read,
            scopes=["alerts:read"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(key_read)

        secret_no_scope, prefix_no_scope, hash_no_scope = generate_api_key()
        key_no_scope = ApiKey(
            workspace_id=seeded_a.workspace_id,
            name="alerts-isolation-live-no-scope-key",
            key_prefix=prefix_no_scope,
            key_hash=hash_no_scope,
            scopes=["catalog:read"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(key_no_scope)
        session.commit()

    try:
        yield _IsolationFixture(
            seeded_a=seeded_a,
            seeded_b=seeded_b,
            api_key_a_read=secret_read,
            api_key_a_no_scope=secret_no_scope,
        )
    finally:
        for seeded in (seeded_a, seeded_b):
            cleanup_alerts_rows(seeded.workspace_id)
        with get_session() as session:
            session.execute(
                text("DELETE FROM api_keys WHERE workspace_id = :ws"),
                {"ws": seeded_a.workspace_id},
            )
            session.commit()
        for seeded in (seeded_a, seeded_b):
            cleanup_seeded_workspace(seeded)


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    from app_shared.config import get_settings

    engine = create_engine(get_settings().DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# --- 1. scoped_select never returns another workspace's row -----------------


def test_scoped_select_never_returns_other_workspace_rows(fixture: _IsolationFixture) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, fixture.seeded_a.workspace_id)

        for model, variant_attr in (
            (VariantPriceState, "product_variant_id"),
            (VariantAlertState, "product_variant_id"),
            (PriceAlertEvent, "product_variant_id"),
        ):
            rows = session.execute(scoped_select(model, fixture.seeded_a.workspace_id)).scalars().all()
            variant_ids = {getattr(row, variant_attr) for row in rows}
            assert fixture.seeded_a.product_variant_id in variant_ids
            assert fixture.seeded_b.product_variant_id not in variant_ids


# --- 2. app-filter-omitted query still returns 0 other-workspace rows (RLS) -


def test_app_filter_omitted_query_returns_zero_other_workspace_rows_via_rls(
    fixture: _IsolationFixture, app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(fixture.seeded_a.workspace_id)},
        )
        for table in ("variant_price_states", "variant_alert_states", "price_alert_events"):
            variant_ids = {
                row[0]
                for row in conn.execute(
                    text(f"SELECT product_variant_id FROM {table}")  # noqa: S608 - fixed allowlist
                ).fetchall()
            }
            assert fixture.seeded_a.product_variant_id in variant_ids
            assert fixture.seeded_b.product_variant_id not in variant_ids


# --- 3. no workspace context at all -> 0 rows, fail closed ------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(
    fixture: _IsolationFixture, app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        for table in ("variant_price_states", "variant_alert_states", "price_alert_events"):
            rows = conn.execute(
                text(
                    f"SELECT product_variant_id FROM {table} "  # noqa: S608 - fixed allowlist
                    "WHERE product_variant_id IN (:a, :b)"
                ),
                {
                    "a": fixture.seeded_a.product_variant_id,
                    "b": fixture.seeded_b.product_variant_id,
                },
            ).fetchall()
            assert rows == []


# --- 4. API-level: workspace A never observes B's rows ----------------------


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_api_never_returns_other_workspace_rows(fixture: _IsolationFixture, client) -> None:
    headers = _auth(fixture.api_key_a_read)

    response = client.get(
        f"/v1/variants/{fixture.seeded_b.product_variant_id}/price-comparison",
        headers=headers,
    )
    assert response.status_code == 404

    response = client.get(
        f"/v1/alerts/current/{fixture.seeded_b.product_variant_id}",
        headers=headers,
    )
    assert response.status_code == 404

    current = client.get("/v1/alerts/current", headers=headers)
    assert current.status_code == 200
    assert all(
        item["product_variant_id"] != str(fixture.seeded_b.product_variant_id)
        for item in current.json()["items"]
    )

    events = client.get("/v1/alert-events", headers=headers)
    assert events.status_code == 200
    assert all(
        item["product_variant_id"] != str(fixture.seeded_b.product_variant_id)
        for item in events.json()["items"]
    )


# --- 5. missing alerts:read scope -> 403 on every alerts route --------------


def test_missing_alerts_read_scope_is_forbidden(fixture: _IsolationFixture, client) -> None:
    headers = _auth(fixture.api_key_a_no_scope)

    response = client.get(
        f"/v1/variants/{fixture.seeded_a.product_variant_id}/price-comparison",
        headers=headers,
    )
    assert response.status_code == 403

    response = client.get("/v1/alerts/current", headers=headers)
    assert response.status_code == 403

    response = client.get(
        f"/v1/alerts/current/{fixture.seeded_a.product_variant_id}", headers=headers
    )
    assert response.status_code == 403

    response = client.get("/v1/alert-events", headers=headers)
    assert response.status_code == 403


def test_unknown_variant_id_is_not_found_not_leaked_as_other_status(
    fixture: _IsolationFixture, client
) -> None:
    headers = _auth(fixture.api_key_a_read)
    response = client.get(
        f"/v1/variants/{uuid.uuid4()}/price-comparison", headers=headers
    )
    assert response.status_code == 404
