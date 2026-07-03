"""Live scrape-to-analysis-to-comparison round trip (SPEC-09 US1/T034,
FR-012/FR-017, SC-002/SC-005) — ⏸ DEFERRED.

`tests/unit/test_price_analysis_task.py` + `tests/unit/test_alerts_router.py`
already prove `recompute_variant`'s upsert shape and the price-comparison
endpoint's response shape exhaustively against fakes. This live test's
distinguishing contribution: a real `variant_price_states`/
`variant_alert_states` upsert (`unique(workspace_id, product_variant_id)`
actually enforced/exercised by Postgres, not just asserted at the app
layer) driven by `recompute_variant` running against real
`match_current_prices` rows, then read back through the real
`GET /v1/variants/{id}/price-comparison` endpoint (`app.main` +
`TestClient`, a real `alerts:read`-scoped API key) — the full seam this
spec adds over SPEC-07.

Per contracts/price-analysis-task.md / contracts/api-alerts.md:

1. Seed one workspace/product/variant + several comparable
   `match_current_prices` rows; run `recompute_variant`; assert
   `variant_price_states` records the correct cheapest/average/highest +
   comparable count and the alert type/severity match the pure engine
   (computed independently in this test via `app_shared.alerts.engine`
   for cross-checking).
2. `GET /v1/variants/{id}/price-comparison` returns exactly those stored
   values.
3. Re-running `recompute_variant` with unchanged inputs yields
   byte-identical `variant_price_states`/`variant_alert_states` values
   (only `calculated_at`/`updated_at` advance) — idempotent (SC-002).

`recompute_variant` runs in its own subprocess
(`_alerts_live_support.run_recompute_variant`) — `apps/api` and
`apps/workers` both ship a top-level `app` package, so importing
`app.workers.tasks_analysis` in the same process as this file's
`app.main` `TestClient` is ambiguous (see
`tests/unit/test_price_analysis_task.py`'s docstring for the precedent).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-09 migration applied)
AND a reachable Redis (`REDIS_URL` — the Celery task decorator/producer
import chain touches it even for a direct, non-`.delay()` call). Not
runnable in the no-Docker-daemon build environment used to author this
feature — SKIPS cleanly whenever either isn't reachable or the SPEC-09
alerts tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host with the SPEC-09 migration applied).
"""

from __future__ import annotations

import uuid
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

    seeded = seed_workspace_with_variant("price-analysis-recompute-live")
    set_variant_price(seeded.product_variant_id, price=Decimal("100.0000"), currency="USD")

    with get_session() as session:
        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=seeded.workspace_id,
            name="price-analysis-recompute-live-key",
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


def _expected_outcome(client_price: Decimal, prices: list[Decimal]):
    from app_shared.alerts import engine
    from app_shared.alerts.engine import CompetitorPrice

    rows = [
        CompetitorPrice(
            match_id=uuid.uuid4(), price=p, currency="USD", success=True, comparable=True
        )
        for p in prices
    ]
    return engine.analyze(client_price, "USD", rows)


def test_recompute_variant_then_price_comparison_round_trip(
    fixture: _Fixture, client
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    competitor_id = seed_competitor(fixture.seeded, "Recompute Live Competitor 2")
    prices = [Decimal("90.0000"), Decimal("95.0000"), Decimal("110.0000")]
    for price in prices:
        match_id = seed_match(
            fixture.seeded, competitor_id, f"https://recompute-live.invalid/p/{price}"
        )
        seed_match_current_price(fixture.seeded, match_id, competitor_id, price=price)

    result = run_recompute_variant(
        workspace_id=fixture.seeded.workspace_id,
        product_variant_id=fixture.seeded.product_variant_id,
        product_id=fixture.seeded.product_id,
    )
    assert result.returncode == 0, (
        f"recompute_variant subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    expected = _expected_outcome(Decimal("100.0000"), prices)

    with get_session() as session:
        row = session.execute(
            text(
                "SELECT client_price, cheapest_competitor_price, average_competitor_price, "
                "highest_competitor_price, comparable_competitor_count, latest_alert_type, "
                "latest_alert_severity FROM variant_price_states WHERE workspace_id = :ws "
                "AND product_variant_id = :variant"
            ),
            {"ws": fixture.seeded.workspace_id, "variant": fixture.seeded.product_variant_id},
        ).one()

    assert row.client_price == Decimal("100.0000")
    assert row.cheapest_competitor_price == expected.cheapest
    assert row.average_competitor_price == expected.average
    assert row.highest_competitor_price == expected.highest
    assert row.comparable_competitor_count == expected.comparable_count
    assert row.latest_alert_type == expected.type.value
    assert row.latest_alert_severity == expected.severity.value

    response = client.get(
        f"/v1/variants/{fixture.seeded.product_variant_id}/price-comparison",
        headers=_auth_headers(fixture),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["product_variant_id"] == str(fixture.seeded.product_variant_id)
    assert Decimal(body["client_price"]) == Decimal("100.0000")
    assert Decimal(body["cheapest_competitor_price"]) == expected.cheapest
    assert Decimal(body["average_competitor_price"]) == expected.average
    assert Decimal(body["highest_competitor_price"]) == expected.highest
    assert body["comparable_competitor_count"] == expected.comparable_count
    assert body["alert_type"] == expected.type.value
    assert body["alert_severity"] == expected.severity.value

    # Re-run with unchanged inputs -> byte-identical state (SC-002); only
    # calculated_at/updated_at advance.
    result2 = run_recompute_variant(
        workspace_id=fixture.seeded.workspace_id,
        product_variant_id=fixture.seeded.product_variant_id,
        product_id=fixture.seeded.product_id,
    )
    assert result2.returncode == 0, (
        f"re-run recompute_variant failed:\nstdout={result2.stdout}\nstderr={result2.stderr}"
    )

    with get_session() as session:
        row2 = session.execute(
            text(
                "SELECT client_price, cheapest_competitor_price, average_competitor_price, "
                "highest_competitor_price, comparable_competitor_count, latest_alert_type, "
                "latest_alert_severity FROM variant_price_states WHERE workspace_id = :ws "
                "AND product_variant_id = :variant"
            ),
            {"ws": fixture.seeded.workspace_id, "variant": fixture.seeded.product_variant_id},
        ).one()

    assert row2._mapping == row._mapping


def test_price_comparison_404s_unknown_or_never_analyzed_variant(
    fixture: _Fixture, client
) -> None:
    unknown_variant_id = uuid.uuid4()
    response = client.get(
        f"/v1/variants/{unknown_variant_id}/price-comparison",
        headers=_auth_headers(fixture),
    )
    assert response.status_code == 404

    # The seeded variant itself, before any recompute_variant run, has no
    # variant_price_states row yet -> also 404 ("never analyzed").
    response2 = client.get(
        f"/v1/variants/{fixture.seeded.product_variant_id}/price-comparison",
        headers=_auth_headers(fixture),
    )
    assert response2.status_code == 404
