"""Live retention-drop test (SPEC-15 US3 T030, contracts/retention-drop.md,
FR-015/016/017/018/019/020; US3 AS-1..4; SC-003/004/005).

Exercises `app_shared.maintenance.retention.run_retention` directly
against a real Postgres, through the same BYPASSRLS `get_system_session`
seam the `MAINTENANCE_RETENTION_DROP` Celery task uses
(`apps/workers/app/workers/tasks_maintenance.py`):

1. An expired `price_observations` partition with COMPLETE daily-rollup
   date coverage is dropped via `DROP TABLE` (AS-1, SC-003) — no bulk
   `DELETE` is ever issued against the raw table (the implementation
   itself never builds one; this test's assertion is behavioral: the
   partition -- and every row it held -- vanishes atomically with the
   `DROP`, and `price_observations` outside that partition is
   untouched).
2. An expired `price_observations` partition with MISSING coverage is
   RETAINED and reported `partitions_skipped_pending_rollups` (AS-2,
   FR-016, SC-004).
3. An in-window partition (current month) is left completely untouched
   (AS-3, FR-018).
4. `request_attempts`/`price_alert_events` (feeds_rollups=False) drop by
   age alone — no coverage check gates them (AS-4, FR-019).
5. Re-running retention over an already-retention-clean state does not
   error or double-drop (FR-020).
6. The one sanctioned bulk `DELETE` ages `variant_price_daily_rollups`
   rows older than `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS` (R7,
   Part B) while newer rows survive.

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-07/09/15 tables
migrated) AND a usable BYPASSRLS system role (`SYSTEM_DATABASE_URL` /
`AUTH_DATABASE_URL` fallback). SKIPS cleanly whenever either isn't
reachable/configured in this build environment (no live Postgres here —
never faked).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

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
        "request_attempts",
        "price_alert_events",
        "variant_price_daily_rollups",
    }
)


def _retention_drop_live_reachable() -> bool:
    """Best-effort probe: Postgres (+ required tables) and a usable
    BYPASSRLS system session, both reachable."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine, get_system_sessionmaker

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_TABLES <= table_names:
            return False

        with get_system_sessionmaker()() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _retention_drop_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-07/09/15 "
        "tables migrated) and a usable BYPASSRLS system role "
        "(SYSTEM_DATABASE_URL / AUTH_DATABASE_URL) in this environment."
    ),
)


# --- raw partition helpers (arbitrary past months, not via create_missing_partitions) --


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _partition_name(parent: str, year: int, month: int) -> str:
    return f"{parent}_{year:04d}_{month:02d}"


def _create_month_partition(parent: str, year: int, month: int) -> str:
    """Create ``parent``'s partition for an arbitrary (possibly far past
    or current) month, directly via the same DDL convention
    `create_missing_partitions` uses — that function only ever creates
    *current + lookahead* months, so an about-to-expire past partition
    for this test is created here instead."""
    from app_shared.database import get_system_sessionmaker

    start, end = _month_bounds(year, month)
    name = _partition_name(parent, year, month)
    with get_system_sessionmaker()() as session:
        session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {parent} "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
        session.commit()
    return name


def _partition_exists(name: str) -> bool:
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        return bool(
            session.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{name}"},
            ).scalar()
        )


def _drop_partition_raw(name: str) -> None:
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        session.execute(text(f"DROP TABLE IF EXISTS {name}"))
        session.commit()


def _run_retention(now_utc: datetime):
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.retention import run_retention

    with get_system_sessionmaker()() as session:
        report = run_retention(session, now_utc=now_utc)
        session.commit()
    return report


def _insert_observation(seeded: SeededWorkspace, *, scraped_at: datetime) -> None:
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
                price=Decimal("10.0000"),
                currency="USD",
                success=True,
                comparable=True,
            )
        )
        session.commit()


def _insert_rollup_row(seeded: SeededWorkspace, *, rollup_date: date) -> None:
    from app_shared.database import get_session
    from app_shared.models.rollups import VariantPriceDailyRollup

    with get_session() as session:
        session.add(
            VariantPriceDailyRollup(
                workspace_id=seeded.workspace_id,
                product_id=seeded.product_id,
                product_variant_id=seeded.product_variant_id,
                date=rollup_date,
                currency="USD",
                client_price=Decimal("10.0000"),
                comparable_competitor_count=0,
            )
        )
        session.commit()


def _delete_rollups(workspace_id: uuid.UUID) -> None:
    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM variant_price_daily_rollups WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.commit()


# --- AS-1/SC-003: expired + rollups complete -> dropped ---------------------


def test_expired_partition_with_complete_coverage_is_dropped_via_drop_table() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    year, month = old.year, old.month
    partition = _create_month_partition("price_observations", year, month)

    seeded = seed_workspace_with_variant("retention-drop-covered")
    obs_ts = datetime(year, month, 15, 12, 0, tzinfo=timezone.utc)
    try:
        _insert_observation(seeded, scraped_at=obs_ts)
        _insert_rollup_row(seeded, rollup_date=obs_ts.date())

        report = _run_retention(datetime.now(timezone.utc))

        assert partition in report.partitions_dropped
        assert not _partition_exists(partition)
        assert partition not in report.partitions_skipped_pending_rollups
    finally:
        _delete_rollups(seeded.workspace_id)
        # The partition (and every row it held) is already gone via
        # DROP; only the workspace/product/variant remain to clean up.
        cleanup_seeded_workspace(seeded)
        _drop_partition_raw(partition)  # no-op if already dropped (FR-020)


# --- AS-2/FR-016/SC-004: expired + rollups incomplete -> retained + flagged -


def test_expired_partition_with_missing_coverage_is_retained_and_flagged() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=410)
    year, month = old.year, old.month
    partition = _create_month_partition("price_observations", year, month)

    seeded = seed_workspace_with_variant("retention-drop-uncovered")
    obs_ts = datetime(year, month, 10, 12, 0, tzinfo=timezone.utc)
    try:
        _insert_observation(seeded, scraped_at=obs_ts)
        # Deliberately NO corresponding variant_price_daily_rollups row.

        report = _run_retention(datetime.now(timezone.utc))

        assert partition in report.partitions_skipped_pending_rollups
        assert partition not in report.partitions_dropped
        assert _partition_exists(partition)
    finally:
        cleanup_seeded_workspace(seeded)
        _drop_partition_raw(partition)


# --- AS-3/FR-018: in-window partition left untouched ------------------------


def test_in_window_partition_is_untouched() -> None:
    now = datetime.now(timezone.utc)
    partition = _create_month_partition("price_observations", now.year, now.month)

    report = _run_retention(now)

    assert partition not in report.partitions_dropped
    assert partition not in report.partitions_skipped_pending_rollups
    assert _partition_exists(partition)


# --- AS-4/FR-019: non-rollup tables drop by age alone -----------------------


def test_non_rollup_tables_drop_by_age_alone() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    year, month = old.year, old.month

    attempts_partition = _create_month_partition("request_attempts", year, month)
    alerts_partition = _create_month_partition("price_alert_events", year, month)

    try:
        report = _run_retention(datetime.now(timezone.utc))

        assert attempts_partition in report.partitions_dropped
        assert alerts_partition in report.partitions_dropped
        assert not _partition_exists(attempts_partition)
        assert not _partition_exists(alerts_partition)
        # Never gated by a coverage check -- feeds_rollups=False (FR-019).
        assert attempts_partition not in report.partitions_skipped_pending_rollups
        assert alerts_partition not in report.partitions_skipped_pending_rollups
    finally:
        _drop_partition_raw(attempts_partition)
        _drop_partition_raw(alerts_partition)


# --- FR-020: re-run does not drop twice / does not error --------------------


def test_rerun_after_drop_does_not_error_or_double_drop() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    year, month = old.year, old.month
    partition = _create_month_partition("request_attempts", year, month)

    first = _run_retention(datetime.now(timezone.utc))
    assert partition in first.partitions_dropped
    assert not _partition_exists(partition)

    # Second pass: the partition is already gone from pg_catalog, so it
    # is simply not discovered again -- no error, no duplicate drop.
    second = _run_retention(datetime.now(timezone.utc))
    assert partition not in second.partitions_dropped


# --- Part B/R7: the one sanctioned bulk DELETE ages the rollup table --------


def test_rollup_table_age_delete_removes_old_rows_keeps_recent() -> None:
    from app_shared.config import get_settings

    settings = get_settings()
    seeded = seed_workspace_with_variant("retention-rollup-age")
    old_date = date.today() - timedelta(days=settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS + 5)
    recent_date = date.today() - timedelta(days=1)
    try:
        _insert_rollup_row(seeded, rollup_date=old_date)
        _insert_rollup_row(seeded, rollup_date=recent_date)

        report = _run_retention(datetime.now(timezone.utc))
        assert report.rollup_rows_deleted >= 1

        from app_shared.database import get_session
        from app_shared.models.rollups import VariantPriceDailyRollup
        from app_shared.repository import scoped_select

        with get_session() as session:
            remaining_dates = {
                row.date
                for row in session.execute(
                    scoped_select(VariantPriceDailyRollup, seeded.workspace_id).where(
                        VariantPriceDailyRollup.product_variant_id == seeded.product_variant_id
                    )
                ).scalars()
            }
        assert old_date not in remaining_dates
        assert recent_date in remaining_dates
    finally:
        _delete_rollups(seeded.workspace_id)
        cleanup_seeded_workspace(seeded)
