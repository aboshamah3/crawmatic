"""Live currency-mismatch write-back (SPEC-09 US3/T037, FR-010, SC-006)
— ⏸ DEFERRED.

`tests/unit/test_price_analysis_task.py`'s
`test_currency_mismatched_competitor_excluded_and_flipped` already
proves the write-back path against a fake session. This live test's
distinguishing contribution: a real `match_current_prices` row, in a
currency that does not match the variant's, actually flipped
`comparable=false` / `error_code='CURRENCY_MISMATCH'` by a real
`recompute_variant` run against Postgres — and that the flip is
idempotent (a second run is a no-op, per contracts/price-analysis-task.md
step 4 — "only flips rows currently comparable").

Per contracts/alert-engine.md `filter_comparable` / contracts/price-
analysis-task.md step 4:

1. Seed a USD-priced variant with two comparable USD competitors and one
   EUR competitor (mismatched). Run `recompute_variant`.
2. `variant_price_states` benchmarks/count reflect only the two USD
   rows — no FX, the mismatched EUR price never enters the
   cheapest/average/highest computation.
3. The EUR `match_current_prices` row is flipped
   `comparable=false`/`error_code='CURRENCY_MISMATCH'`.
4. Re-running `recompute_variant` is idempotent — the already-flipped
   row's `comparable`/`error_code` are unchanged (no re-flip, no error),
   and the benchmarks/count are unchanged.

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
    usd_match_ids: list
    eur_match_id: object


@pytest.fixture()
def fixture() -> Iterator[_Fixture]:
    seeded = seed_workspace_with_variant("currency-mismatch-live")
    set_variant_price(seeded.product_variant_id, price=Decimal("100.0000"), currency="USD")
    competitor_id = seed_competitor(seeded, "Currency Mismatch Live Competitor")

    usd_match_ids = []
    for price in (Decimal("90.0000"), Decimal("110.0000")):
        match_id = seed_match(
            seeded, competitor_id, f"https://currency-mismatch-live.invalid/usd/{price}"
        )
        seed_match_current_price(seeded, match_id, competitor_id, price=price, currency="USD")
        usd_match_ids.append(match_id)

    eur_match_id = seed_match(
        seeded, competitor_id, "https://currency-mismatch-live.invalid/eur/1"
    )
    seed_match_current_price(
        seeded, eur_match_id, competitor_id, price=Decimal("80.0000"), currency="EUR"
    )

    try:
        yield _Fixture(seeded=seeded, usd_match_ids=usd_match_ids, eur_match_id=eur_match_id)
    finally:
        cleanup_alerts_rows(seeded.workspace_id)
        cleanup_seeded_workspace(seeded)


def _match_current_price_row(match_id):
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        return session.execute(
            text(
                "SELECT comparable, error_code, currency FROM match_current_prices "
                "WHERE match_id = :m"
            ),
            {"m": match_id},
        ).one()


def _variant_price_state_row(seeded: SeededWorkspace):
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        return session.execute(
            text(
                "SELECT cheapest_competitor_price, average_competitor_price, "
                "highest_competitor_price, comparable_competitor_count "
                "FROM variant_price_states WHERE workspace_id = :ws AND product_variant_id = :v"
            ),
            {"ws": seeded.workspace_id, "v": seeded.product_variant_id},
        ).one()


def test_mismatched_currency_competitor_excluded_and_flipped(fixture: _Fixture) -> None:
    result = run_recompute_variant(
        workspace_id=fixture.seeded.workspace_id,
        product_variant_id=fixture.seeded.product_variant_id,
        product_id=fixture.seeded.product_id,
    )
    assert result.returncode == 0, (
        f"recompute_variant subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    price_state = _variant_price_state_row(fixture.seeded)
    # Only the two USD rows (90, 110) feed the benchmarks -- no FX applied
    # to the EUR row, and it is excluded from the count entirely.
    assert price_state.cheapest_competitor_price == Decimal("90.0000")
    assert price_state.highest_competitor_price == Decimal("110.0000")
    assert price_state.average_competitor_price == Decimal("100.0000")
    assert price_state.comparable_competitor_count == 2

    eur_row = _match_current_price_row(fixture.eur_match_id)
    assert eur_row.comparable is False
    assert eur_row.error_code == "CURRENCY_MISMATCH"
    assert eur_row.currency == "EUR"

    for match_id in fixture.usd_match_ids:
        usd_row = _match_current_price_row(match_id)
        assert usd_row.comparable is True
        assert usd_row.error_code is None

    # Idempotent re-run: the already-flipped EUR row is untouched (no
    # re-flip, no error), and benchmarks/count are unchanged.
    result2 = run_recompute_variant(
        workspace_id=fixture.seeded.workspace_id,
        product_variant_id=fixture.seeded.product_variant_id,
        product_id=fixture.seeded.product_id,
    )
    assert result2.returncode == 0, (
        f"re-run recompute_variant failed:\nstdout={result2.stdout}\nstderr={result2.stderr}"
    )

    price_state2 = _variant_price_state_row(fixture.seeded)
    assert price_state2 == price_state

    eur_row2 = _match_current_price_row(fixture.eur_match_id)
    assert eur_row2.comparable is False
    assert eur_row2.error_code == "CURRENCY_MISMATCH"
