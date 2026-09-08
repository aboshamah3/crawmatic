"""Live proof of the set-based daily rollup (EPA C7, F12).

Three claims that only a real Postgres can settle, because they are
claims about what the ``WITH ... DISTINCT ON ... INSERT ... SELECT``
statement in `app_shared.maintenance.rollup_sql` actually *means*:

1. **The 10-vs-1 observation skew is gone.** A competitor whose page was
   scraped ten times that day counts ONCE, at its latest usable price.
   Before C7 the rollup averaged every reading, so a variant's daily
   "average competitor price" was weighted by how often each competitor
   happened to be refreshed -- an artefact of the scrape schedule, not
   of the market, and one that could put the average below any price a
   human could have paid.
2. **The averages match a hand computation.** The oracle is
   `aggregate_competitor_prices`, the pure Python statement of the same
   rule, run over the same seeded rows -- not a number copied out of a
   previous run.
3. **A crash mid-batch resumes from the checkpoint without double
   counting.** The run is interrupted between batches, the durable
   `rollup_completion` cursor is inspected, and the resumed run is shown
   to produce exactly the rows a single uninterrupted run produces --
   with `comparable_competitor_count` unchanged, which is the number an
   additive (rather than absolute) upsert would have doubled.

Needs a reachable Postgres with the SPEC-07/09/15 tables AND
`rollup_completion` migrated, a reachable Redis (the `recompute_variant`
seam), and a usable BYPASSRLS system role. SKIPS cleanly otherwise --
never faked.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

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
        "price_observations",
        "variant_price_states",
        "variant_price_daily_rollups",
        "rollup_completion",
    }
)


def _reachable() -> bool:
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import (
            check_connection,
            get_engine,
            get_system_sessionmaker,
        )
        from app_shared.redis_client import get_redis_client

        check_connection()
        if not _REQUIRED_TABLES <= set(inspect(get_engine()).get_table_names()):
            return False
        get_redis_client().ping()
        with get_system_sessionmaker()() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _reachable(),
        reason=(
            "Needs a reachable Postgres with the SPEC-07/09/15 tables and "
            "rollup_completion migrated, a reachable Redis, and a usable "
            "BYPASSRLS system role."
        ),
    ),
]


# --- seeding --------------------------------------------------------------


def _seed_variant(name_prefix: str, *, price: Decimal, currency: str) -> SeededWorkspace:
    seeded = seed_workspace_with_variant(name_prefix)
    set_variant_price(seeded.product_variant_id, price=price, currency=currency)
    result = run_recompute_variant(
        workspace_id=seeded.workspace_id,
        product_variant_id=seeded.product_variant_id,
        product_id=seeded.product_id,
    )
    assert result.returncode == 0, result.stderr
    return seeded


def _observe(
    seeded: SeededWorkspace,
    *,
    match_id: uuid.UUID,
    scraped_at: datetime,
    price: Decimal | None,
    currency: str | None = "USD",
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
                match_id=match_id,
                product_id=seeded.product_id,
                product_variant_id=seeded.product_variant_id,
                price=price,
                currency=currency,
                success=success,
                comparable=comparable,
            )
        )
        session.commit()


def _cleanup(seeded: SeededWorkspace, day: date) -> None:
    from app_shared.database import get_session

    with get_session() as session:
        for table in (
            "variant_price_daily_rollups",
            "variant_alert_states",
            "variant_price_states",
        ):
            session.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
        session.commit()
    _clear_completion(day)
    cleanup_seeded_workspace(seeded)


def _clear_completion(day: date) -> None:
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        session.execute(
            text("DELETE FROM rollup_completion WHERE date = :day"), {"day": day}
        )
        session.commit()


def _rollup_row(seeded: SeededWorkspace, day: date):
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        return session.execute(
            text(
                """
                SELECT cheapest_competitor_price, average_competitor_price,
                       highest_competitor_price, comparable_competitor_count,
                       client_price, currency
                FROM variant_price_daily_rollups
                WHERE workspace_id = :ws AND product_variant_id = :variant
                  AND date = :day
                """
            ),
            {
                "ws": seeded.workspace_id,
                "variant": seeded.product_variant_id,
                "day": day,
            },
        ).first()


def _rollup_row_count(seeded: SeededWorkspace, day: date) -> int:
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        return session.execute(
            text(
                """
                SELECT count(*) FROM variant_price_daily_rollups
                WHERE workspace_id = :ws AND date = :day
                """
            ),
            {"ws": seeded.workspace_id, "day": day},
        ).scalar_one()


def _completion(day: date):
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.rollup_sql import read_completion

    with get_system_sessionmaker()() as session:
        return read_completion(session, day)


def _run(day: date, **kwargs):
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.rollups import run_daily_rollup

    with get_system_sessionmaker()() as session:
        return run_daily_rollup(session, target_date=day, **kwargs)


@pytest.fixture()
def day() -> date:
    # A fixed past day, never "today" -- no midnight-boundary flakiness.
    return (datetime.now(timezone.utc) - timedelta(days=4)).date()


@pytest.fixture()
def noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


@pytest.fixture()
def variant(day: date):
    seeded = _seed_variant("c7-set-based", price=Decimal("100.0000"), currency="USD")
    try:
        yield seeded
    finally:
        _cleanup(seeded, day)


# --- 1. the 10-vs-1 observation skew --------------------------------------


def test_a_competitor_observed_ten_times_counts_once(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    noisy = uuid.uuid4()
    quiet = uuid.uuid4()
    for hour in range(10):
        _observe(
            variant,
            match_id=noisy,
            scraped_at=noon.replace(hour=hour),
            price=Decimal("10.0000"),
        )
    _observe(variant, match_id=quiet, scraped_at=noon, price=Decimal("30.0000"))

    report = _run(day)

    assert report.rollups_upserted == 1
    row = _rollup_row(variant, day)
    assert row.comparable_competitor_count == 2, (
        "the count must be COMPETITORS, not readings"
    )
    assert row.average_competitor_price == Decimal("20.0000"), (
        "ten readings of the cheap competitor dragged the average "
        f"(got {row.average_competitor_price}, pre-C7 behaviour was 11.8182)"
    )
    assert row.cheapest_competitor_price == Decimal("10.0000")
    assert row.highest_competitor_price == Decimal("30.0000")


def test_the_surviving_reading_per_competitor_is_the_latest_eligible_one(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    match = uuid.uuid4()
    _observe(variant, match_id=match, scraped_at=noon.replace(hour=1), price=Decimal("10.0000"))
    _observe(variant, match_id=match, scraped_at=noon.replace(hour=9), price=Decimal("99.0000"))
    # The competitor's LAST read of the day failed. "Latest eligible"
    # means the 09:00 price survives -- the competitor does not vanish
    # from the day because a scrape errored near midnight.
    _observe(
        variant,
        match_id=match,
        scraped_at=noon.replace(hour=23),
        price=None,
        currency=None,
        success=False,
        comparable=False,
    )

    _run(day)

    row = _rollup_row(variant, day)
    assert row.comparable_competitor_count == 1
    assert row.cheapest_competitor_price == Decimal("99.0000")
    assert row.highest_competitor_price == Decimal("99.0000")


# --- 2. averages match a hand computation ---------------------------------


def test_aggregates_match_the_pure_reference_implementation(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    """The oracle is `aggregate_competitor_prices` -- the same rule
    expressed in Python -- run over the same rows. If the SQL and the
    documented semantics ever diverge, this fails."""
    from app_shared.maintenance.rollups import ObservationRow, aggregate_competitor_prices

    seeded_rows: list[ObservationRow] = []
    plan = [
        # (match, hour, price, currency, success, comparable)
        (uuid.uuid4(), 3, "19.9900", "USD", True, True),
        (uuid.uuid4(), 4, "20.0100", "USD", True, True),
        (uuid.uuid4(), 5, "31.5000", "USD", True, True),
        # Same competitor as the first, read again later and cheaper.
        (None, 20, "18.7500", "USD", True, True),
        # Excluded: currency mismatch, failure, and a non-comparable row.
        (uuid.uuid4(), 6, "1.0000", "SAR", True, False),
        (uuid.uuid4(), 7, None, None, False, False),
        (uuid.uuid4(), 8, "999.0000", "USD", True, False),
    ]
    first_match = plan[0][0]
    for match, hour, price, currency, success, comparable in plan:
        match_id = first_match if match is None else match
        value = None if price is None else Decimal(price)
        _observe(
            variant,
            match_id=match_id,
            scraped_at=noon.replace(hour=hour),
            price=value,
            currency=currency,
            success=success,
            comparable=comparable,
        )
        seeded_rows.append(
            ObservationRow(
                price=value,
                currency=currency,
                success=success,
                comparable=comparable,
                match_id=match_id,
                scraped_at=noon.replace(hour=hour),
            )
        )

    expected = aggregate_competitor_prices(seeded_rows, client_currency="USD")
    # Sanity: the hand computation itself must show the collapse (four
    # eligible rows, three competitors).
    assert expected.comparable_count == 3
    assert expected.average == Decimal("23.4200")

    _run(day)

    row = _rollup_row(variant, day)
    assert row.cheapest_competitor_price == expected.cheapest
    assert row.average_competitor_price == expected.average
    assert row.highest_competitor_price == expected.highest
    assert row.comparable_competitor_count == expected.comparable_count


def test_a_variant_with_no_eligible_observation_still_gets_a_row(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    """FR-013 / US2 AS-3: count 0 with NULL competitor prices, not an
    omitted row -- the LEFT JOIN in the statement."""
    _observe(
        variant,
        match_id=uuid.uuid4(),
        scraped_at=noon,
        price=Decimal("5.0000"),
        currency="SAR",
        comparable=False,
    )

    _run(day)

    row = _rollup_row(variant, day)
    assert row is not None
    assert row.comparable_competitor_count == 0
    assert row.cheapest_competitor_price is None
    assert row.average_competitor_price is None
    assert row.highest_competitor_price is None
    assert row.client_price == Decimal("100.0000")


# --- 3. crash mid-batch resumes without double counting -------------------


def test_an_interrupted_run_resumes_from_the_checkpoint_without_double_counting(
    day: date, noon: datetime
) -> None:
    """Three variants, one per batch, interrupted after the first.

    The resumed run must finish the remaining variants and leave EXACTLY
    the rows an uninterrupted run leaves -- in particular
    `comparable_competitor_count` unchanged, which is the number an
    additive upsert (`count + delta`) would have doubled for the variant
    that was already written when the interruption happened.
    """
    seeded = [
        _seed_variant(f"c7-resume-{index}", price=Decimal("100.0000"), currency="USD")
        for index in range(3)
    ]
    try:
        for variant_seed in seeded:
            for offset, price in enumerate(("10.0000", "20.0000", "30.0000")):
                _observe(
                    variant_seed,
                    match_id=uuid.uuid4(),
                    scraped_at=noon.replace(hour=offset + 1),
                    price=Decimal(price),
                )

        # --- the interrupted run: one variant, then stop ---------------
        first = _run(day, batch_limit=1, max_batches=1)
        assert first.complete is False
        assert first.batches == 1
        assert first.rollups_upserted == 1

        checkpoint = _completion(day)
        assert checkpoint is not None
        assert checkpoint.complete is False
        assert checkpoint.last_key_workspace_id is not None
        written_after_crash = sum(_rollup_row_count(s, day) for s in seeded)
        assert written_after_crash == 1, "the finished batch must be durable"

        # --- the resumed run -------------------------------------------
        second = _run(day, batch_limit=1)
        assert second.complete is True
        assert second.resumed_from == (
            str(checkpoint.last_key_workspace_id),
            str(checkpoint.last_key_variant_id),
        )
        # It must NOT redo the variant the first run already wrote.
        assert second.rollups_upserted == 2

        resumed_checkpoint = _completion(day)
        assert resumed_checkpoint.complete is True

        # --- every variant has exactly one correct row -----------------
        for variant_seed in seeded:
            assert _rollup_row_count(variant_seed, day) == 1
            row = _rollup_row(variant_seed, day)
            assert row.comparable_competitor_count == 3, (
                "an additive upsert would have doubled this for the "
                "variant written before the interruption"
            )
            assert row.average_competitor_price == Decimal("20.0000")
            assert row.cheapest_competitor_price == Decimal("10.0000")
            assert row.highest_competitor_price == Decimal("30.0000")
    finally:
        for variant_seed in seeded:
            _cleanup(variant_seed, day)


def test_a_day_already_complete_is_a_no_op_and_a_restart_re_derives_it(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    _observe(variant, match_id=uuid.uuid4(), scraped_at=noon, price=Decimal("42.0000"))

    first = _run(day)
    assert first.complete is True
    assert first.rollups_upserted == 1

    # Same day again: the checkpoint says complete, so nothing is touched.
    second = _run(day)
    assert second.rollups_upserted == 0
    assert second.batches == 0

    # A deliberate repair re-derives it, and the row is unchanged
    # (absolute upsert, never additive).
    third = _run(day, restart=True)
    assert third.rollups_upserted == 1
    assert _rollup_row_count(variant, day) == 1
    assert _rollup_row(variant, day).comparable_competitor_count == 1


def test_a_dry_run_writes_nothing_at_all(
    variant: SeededWorkspace, day: date, noon: datetime
) -> None:
    """`scripts/backfill_daily_rollups.py` holds `SET TRANSACTION READ
    ONLY` for the whole call, which only works if no write is attempted."""
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.rollups import run_daily_rollup

    _observe(variant, match_id=uuid.uuid4(), scraped_at=noon, price=Decimal("42.0000"))

    with get_system_sessionmaker()() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        report = run_daily_rollup(session, target_date=day, dry_run=True)

    assert report.rollups_upserted == 1
    assert _rollup_row_count(variant, day) == 0
    assert _completion(day) is None
