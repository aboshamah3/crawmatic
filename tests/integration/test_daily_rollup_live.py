"""Live daily-rollup test (SPEC-15 US2 T023, contracts/daily-rollup.md,
FR-009/010/011/012/013/014; US2 AS-1..4; SC-002/006).

Exercises `app_shared.maintenance.rollups.run_daily_rollup` directly
against a real Postgres, through the same BYPASSRLS `get_system_session`
seam the `MAINTENANCE_DAILY_ROLLUP` Celery task uses
(`apps/workers/app/workers/tasks_maintenance.py`):

1. One `variant_price_daily_rollups` row per (workspace, variant, day)
   with the correct client price, competitor min/avg/max, comparable
   count, and alert type (AS-1).
2. Currency-mismatched competitor prices are excluded from both the
   aggregate AND the count (AS-4, FR-011, SC-006).
3. A variant with observations that day but zero comparable ones still
   gets a row: client price kept, competitor prices NULL, count 0
   (AS-3, FR-013).
4. Re-running the same day upserts in place — no duplicate row, no
   corruption (AS-2, FR-010, SC-002).
5. A cross-workspace RLS-denial check on the new table (FR-014).

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-07/09/15 tables
migrated) AND a reachable Redis (`REDIS_URL` — `recompute_variant`
touches it via the Celery task import chain even for a direct call,
mirroring `_alerts_live_support.alerts_live_reachable`) AND a usable
BYPASSRLS system role. SKIPS cleanly whenever any of these aren't
reachable/configured in this build environment (no live Postgres here —
never faked).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ._alerts_live_support import run_recompute_variant, set_variant_price
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = frozenset(
    {
        "workspaces",
        "products",
        "product_variants",
        "competitors",
        "competitor_product_matches",
        "match_current_prices",
        "variant_price_states",
        "price_observations",
        "variant_price_daily_rollups",
    }
)


def _daily_rollup_live_reachable() -> bool:
    """Best-effort probe: Postgres (+ required tables), Redis, and a
    usable BYPASSRLS system session, all reachable."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine, get_system_sessionmaker
        from app_shared.redis_client import get_redis_client

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_TABLES <= table_names:
            return False

        get_redis_client().ping()

        system_sessionmaker = get_system_sessionmaker()
        with system_sessionmaker() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _daily_rollup_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-07/09/15 tables "
        "migrated), a reachable Redis (REDIS_URL), and a usable BYPASSRLS "
        "system role in this environment."
    ),
)


def _database_url() -> str | None:
    import os

    return os.environ.get("DATABASE_URL")


def _cleanup_rollup_surface(workspace_id: uuid.UUID) -> None:
    """Delete `variant_price_daily_rollups`/`variant_price_states`/
    `variant_alert_states` rows for `workspace_id` — must run BEFORE
    `cleanup_seeded_workspace` (the workspace FK has no `ondelete`)."""
    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM variant_price_daily_rollups WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM variant_alert_states WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM variant_price_states WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.commit()


def _seed_variant_with_state(name_prefix: str, *, price: Decimal, currency: str) -> SeededWorkspace:
    """One workspace/product/variant with a SPEC-09 `variant_price_states`
    row (via the real `recompute_variant`, zero `match_current_prices`
    rows -- `NO_COMPETITOR_DATA`), ready to have raw `price_observations`
    seeded directly for the rollup job to aggregate."""
    seeded = seed_workspace_with_variant(name_prefix)
    set_variant_price(seeded.product_variant_id, price=price, currency=currency)
    result = run_recompute_variant(
        workspace_id=seeded.workspace_id,
        product_variant_id=seeded.product_variant_id,
        product_id=seeded.product_id,
    )
    assert result.returncode == 0, result.stderr
    return seeded


def _insert_observation(
    seeded: SeededWorkspace,
    *,
    scraped_at: datetime,
    price: Decimal | None,
    currency: str | None,
    success: bool = True,
    comparable: bool = True,
) -> None:
    from app_shared.database import get_session
    from app_shared.models.observations import PriceObservation

    with get_session() as session:
        session.add(
            PriceObservation(
                workspace_id=seeded.workspace_id,
                scraped_at=scraped_at,
                match_id=uuid.uuid4(),
                product_id=seeded.product_id,
                product_variant_id=seeded.product_variant_id,
                price=price,
                currency=currency,
                success=success,
                comparable=comparable,
            )
        )
        session.commit()


def _fetch_rollup(workspace_id: uuid.UUID, product_variant_id: uuid.UUID, target_date: date):
    from app_shared.database import get_session
    from app_shared.models.rollups import VariantPriceDailyRollup
    from app_shared.repository import scoped_select

    with get_session() as session:
        return session.execute(
            scoped_select(VariantPriceDailyRollup, workspace_id).where(
                VariantPriceDailyRollup.product_variant_id == product_variant_id,
                VariantPriceDailyRollup.date == target_date,
            )
        ).scalar_one_or_none()


def _run_rollup(target_date: date):
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.rollups import run_daily_rollup

    with get_system_sessionmaker()() as session:
        report = run_daily_rollup(session, target_date=target_date)
        session.commit()
    return report


# --- AS-1/AS-4/SC-006: correct row; currency mismatch excluded --------------


@pytest.fixture()
def rollup_day() -> date:
    # A fixed, deterministic past day -- never "today" (avoids any
    # midnight-boundary flakiness with `default_target_date`, which this
    # test never exercises since `target_date` is always explicit here).
    return (datetime.now(timezone.utc) - timedelta(days=3)).date()


@pytest.fixture()
def seeded_variant():
    seeded = _seed_variant_with_state(
        "daily-rollup-live", price=Decimal("100.0000"), currency="USD"
    )
    try:
        yield seeded
    finally:
        _cleanup_rollup_surface(seeded.workspace_id)
        cleanup_seeded_workspace(seeded)


def test_correct_row_with_currency_mismatch_excluded_and_rerun_upserts(
    seeded_variant: SeededWorkspace, rollup_day: date
) -> None:
    day_ts = datetime(rollup_day.year, rollup_day.month, rollup_day.day, 12, 0, tzinfo=timezone.utc)

    # Three comparable, same-currency (USD) observations.
    for price in (Decimal("10.0000"), Decimal("12.0000"), Decimal("14.0000")):
        _insert_observation(seeded_variant, scraped_at=day_ts, price=price, currency="USD")

    # One currency-mismatched observation (SPEC-09 already flips
    # `comparable=False` on a currency mismatch) -- must be excluded from
    # both the aggregate and the count (AS-4, FR-011, SC-006).
    _insert_observation(
        seeded_variant,
        scraped_at=day_ts,
        price=Decimal("1.0000"),
        currency="EUR",
        comparable=False,
    )

    report = _run_rollup(rollup_day)
    assert report.rollups_upserted == 1

    row = _fetch_rollup(seeded_variant.workspace_id, seeded_variant.product_variant_id, rollup_day)
    assert row is not None
    assert row.client_price == Decimal("100.0000")
    assert row.currency == "USD"
    assert row.cheapest_competitor_price == Decimal("10.0000")
    assert row.average_competitor_price == Decimal("12.0000")
    assert row.highest_competitor_price == Decimal("14.0000")
    assert row.comparable_competitor_count == 3
    assert row.latest_alert_type is not None

    # --- AS-2/FR-010/SC-002: re-running the same day upserts, no dup -------
    report_again = _run_rollup(rollup_day)
    assert report_again.rollups_upserted == 1

    with_session_rows = _fetch_rollup(
        seeded_variant.workspace_id, seeded_variant.product_variant_id, rollup_day
    )
    assert with_session_rows is not None
    assert with_session_rows.id == row.id  # same row, updated in place
    assert with_session_rows.comparable_competitor_count == 3


# --- AS-3/FR-013: zero comparable competitors -> row with count 0 ----------


@pytest.fixture()
def zero_comparable_variant():
    seeded = _seed_variant_with_state(
        "daily-rollup-zero-live", price=Decimal("50.0000"), currency="USD"
    )
    try:
        yield seeded
    finally:
        _cleanup_rollup_surface(seeded.workspace_id)
        cleanup_seeded_workspace(seeded)


def test_zero_comparable_variant_gets_row_with_count_zero(
    zero_comparable_variant: SeededWorkspace, rollup_day: date
) -> None:
    day_ts = datetime(rollup_day.year, rollup_day.month, rollup_day.day, 9, 0, tzinfo=timezone.utc)

    # Observations exist that day, but none comparable/same-currency.
    _insert_observation(
        zero_comparable_variant,
        scraped_at=day_ts,
        price=Decimal("5.0000"),
        currency="EUR",
        comparable=False,
    )
    _insert_observation(
        zero_comparable_variant,
        scraped_at=day_ts,
        price=None,
        currency=None,
        success=False,
        comparable=False,
    )

    report = _run_rollup(rollup_day)
    assert report.rollups_upserted == 1

    row = _fetch_rollup(
        zero_comparable_variant.workspace_id,
        zero_comparable_variant.product_variant_id,
        rollup_day,
    )
    assert row is not None
    assert row.client_price == Decimal("50.0000")
    assert row.comparable_competitor_count == 0
    assert row.cheapest_competitor_price is None
    assert row.average_competitor_price is None
    assert row.highest_competitor_price is None


# --- FR-014: cross-workspace RLS denial on the new table --------------------


@pytest.fixture()
def app_engine() -> Engine:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


def test_cross_workspace_rls_denies_other_workspace_rollup_row(
    seeded_variant: SeededWorkspace, rollup_day: date, app_engine: Engine
) -> None:
    day_ts = datetime(rollup_day.year, rollup_day.month, rollup_day.day, 15, 0, tzinfo=timezone.utc)
    _insert_observation(seeded_variant, scraped_at=day_ts, price=Decimal("20.0000"), currency="USD")
    _run_rollup(rollup_day)

    other_workspace_id = uuid.uuid4()
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(other_workspace_id)},
        )
        rows = conn.execute(
            text(
                "SELECT id FROM variant_price_daily_rollups WHERE product_variant_id = :pv"
            ),
            {"pv": seeded_variant.product_variant_id},
        ).fetchall()

    assert rows == []
