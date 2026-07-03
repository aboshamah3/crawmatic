"""Live alert-event-history transitions on real rows (SPEC-09 US2/T035,
FR-013/FR-014/FR-018/FR-019, SC-004) — ⏸ DEFERRED.

`tests/unit/test_price_analysis_task.py`'s
`test_event_transition_sequence_created_resolved_reopened_unchanged`
already proves the event-write path exhaustively against a fake
session. This live test's distinguishing contribution: driving
`recompute_variant` repeatedly against **real** `match_current_prices`
rows/a real variant (mutating only the variant's `current_price`
between runs, exactly as that unit test does) so the append-only
`price_alert_events` history is built from genuine Postgres inserts,
then reading it back through the real `/v1/alert-events` +
`/v1/alerts/current` endpoints.

Fixed comparable set (cheapest=100, average=101, highest=104 — four
matches at 100/100/100/104), only the variant's `current_price`
changing between calls (mirrors the unit test precedent exactly, so the
expected type/event at each step is already proven correct):

1. `current_price=102` -> HIGH_PRICE (cheapest 100 < 102 <= highest 104).
   No prior row -> **CREATED**.
2. `current_price=97` -> NORMAL (<=cheapest 100; discount vs avg 101 is
   (101-97)/101*100 = 3.9604%, in [1,5]). prior=HIGH_PRICE -> **RESOLVED**.
3. `current_price=102` again -> HIGH_PRICE. prior=NORMAL,
   had_history=True -> **REOPENED**.
4. Unchanged re-run (`current_price` still 102) -> **zero** new events,
   only `last_seen_at` advances.

`recompute_variant` runs in its own subprocess (see
`_alerts_live_support.run_recompute_variant`'s docstring for why).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-09 migration applied)
AND a reachable Redis (`REDIS_URL`). Not runnable in the no-Docker-daemon
build environment used to author this feature — SKIPS cleanly whenever
either isn't reachable or the SPEC-09 alerts tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host with the SPEC-09 migration applied).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest

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
class _Fixture:
    seeded: SeededWorkspace
    api_key: str


@pytest.fixture()
def fixture() -> Iterator[_Fixture]:
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus
    from app_shared.models import ApiKey
    from app_shared.security.api_keys import generate_api_key

    seeded = seed_workspace_with_variant("alert-events-history-live")
    competitor_id = seed_competitor(seeded, "Alert Events Live Competitor")

    # Fixed comparable set: cheapest=100, average=101, highest=104.
    for price in (Decimal("100.0000"), Decimal("100.0000"), Decimal("100.0000"), Decimal("104.0000")):
        match_id = seed_match(seeded, competitor_id, f"https://alert-events-live.invalid/p/{price}")
        seed_match_current_price(seeded, match_id, competitor_id, price=price)

    with get_session() as session:
        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=seeded.workspace_id,
            name="alert-events-history-live-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["alerts:read"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    try:
        yield _Fixture(seeded=seeded, api_key=full_secret)
    finally:
        cleanup_alerts_rows(seeded.workspace_id)
        from sqlalchemy import text

        with get_session() as session:
            session.execute(
                text("DELETE FROM api_keys WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.commit()
        cleanup_seeded_workspace(seeded)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _auth_headers(fixture: _Fixture) -> dict[str, str]:
    return {"Authorization": f"Bearer {fixture.api_key}"}


def _recompute(fixture: _Fixture, price: Decimal) -> None:
    set_variant_price(fixture.seeded.product_variant_id, price=price, currency="USD")
    result = run_recompute_variant(
        workspace_id=fixture.seeded.workspace_id,
        product_variant_id=fixture.seeded.product_variant_id,
        product_id=fixture.seeded.product_id,
    )
    assert result.returncode == 0, (
        f"recompute_variant subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Postgres timestamp resolution/ordering safety net between runs.
    time.sleep(0.01)


def test_high_price_normal_high_price_unchanged_sequence_writes_expected_events(
    fixture: _Fixture, client
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    variant_id = fixture.seeded.product_variant_id
    ws_id = fixture.seeded.workspace_id

    # Step 1: HIGH_PRICE, no prior row -> CREATED.
    _recompute(fixture, Decimal("102.0000"))
    # Step 2: NORMAL -> RESOLVED.
    _recompute(fixture, Decimal("97.0000"))
    # Step 3: HIGH_PRICE again -> REOPENED.
    _recompute(fixture, Decimal("102.0000"))

    with get_session() as session:
        last_seen_before = session.execute(
            text(
                "SELECT last_seen_at FROM variant_alert_states WHERE workspace_id = :ws "
                "AND product_variant_id = :variant"
            ),
            {"ws": ws_id, "variant": variant_id},
        ).scalar_one()

    # Step 4: unchanged re-run -> zero new events, only last_seen_at advances.
    _recompute(fixture, Decimal("102.0000"))

    with get_session() as session:
        events = session.execute(
            text(
                "SELECT event_type, previous_type, new_type FROM price_alert_events "
                "WHERE workspace_id = :ws AND product_variant_id = :variant "
                "ORDER BY created_at, id"
            ),
            {"ws": ws_id, "variant": variant_id},
        ).all()
        alert_state = session.execute(
            text(
                "SELECT status, resolved_at, last_seen_at FROM variant_alert_states "
                "WHERE workspace_id = :ws AND product_variant_id = :variant"
            ),
            {"ws": ws_id, "variant": variant_id},
        ).one()

    assert [row.event_type for row in events] == ["CREATED", "RESOLVED", "REOPENED"]
    assert events[0].previous_type is None
    assert events[0].new_type == "HIGH_PRICE"
    assert events[1].previous_type == "HIGH_PRICE"
    assert events[1].new_type == "NORMAL"
    assert events[2].previous_type == "NORMAL"
    assert events[2].new_type == "HIGH_PRICE"

    assert alert_state.status == "ACTIVE"
    assert alert_state.resolved_at is None
    assert alert_state.last_seen_at > last_seen_before

    response = client.get(
        "/v1/alert-events",
        params={"variant_id": str(variant_id)},
        headers=_auth_headers(fixture),
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["event_type"] for item in body["items"]] == ["CREATED", "RESOLVED", "REOPENED"]

    current = client.get(
        "/v1/alerts/current",
        params={"type": "HIGH_PRICE"},
        headers=_auth_headers(fixture),
    )
    assert current.status_code == 200
    assert any(
        item["product_variant_id"] == str(variant_id) for item in current.json()["items"]
    )

    no_match = client.get(
        "/v1/alerts/current",
        params={"severity": "CRITICAL"},
        headers=_auth_headers(fixture),
    )
    assert no_match.status_code == 200
    assert all(
        item["product_variant_id"] != str(variant_id) for item in no_match.json()["items"]
    )
