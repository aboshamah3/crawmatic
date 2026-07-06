"""Live partition-creation test (SPEC-15 US1 T013, contracts/
partition-creation.md, FR-002/004/005/006/007/008; US1 AS-1..4;
SC-001/002).

Exercises `app_shared.maintenance.partitions.create_missing_partitions`
directly against a real Postgres, through the same BYPASSRLS
`get_system_session` seam the `MAINTENANCE_PARTITION_CREATE` Celery task
uses (`apps/workers/app/workers/tasks_maintenance.py`):

1. Current + next-month partitions exist for every *existing* registered
   table after one run (AS-1, SC-001).
2. Re-running is a no-op -- no error, no duplicate partitions (AS-2,
   FR-006, SC-002).
3. A missing current-month partition (dropped out-of-band) is
   self-healed by the next run (AS-3, FR-005).
4. `webhook_events` (registered but not yet migrated, SPEC-16) is
   skipped without error (AS-4, FR-002).
5. A write dated into next month succeeds once its partition exists
   (the calendar-driven-outage guarantee this feature exists for).

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-07/09 partitioned
tables migrated) AND a usable BYPASSRLS system role
(`SYSTEM_DATABASE_URL` / `AUTH_DATABASE_URL` fallback). SKIPS cleanly
whenever either isn't reachable/configured in this build environment (no
live Postgres here -- never faked).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ._scrapyd_spider_live_support import (
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = frozenset(
    {
        "workspaces",
        "products",
        "product_variants",
        "competitors",
        "competitor_product_matches",
        "price_observations",
        "request_attempts",
        "price_alert_events",
    }
)


def _live_partition_create_reachable() -> bool:
    """Best-effort probe: Postgres (+ required partitioned tables) and a
    usable BYPASSRLS system session, both reachable."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect, text

        from app_shared.database import check_connection, get_engine, get_system_sessionmaker

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_TABLES <= table_names:
            return False

        system_sessionmaker = get_system_sessionmaker()
        with system_sessionmaker() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_partition_create_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-07/09 "
        "partitioned tables migrated) and a usable BYPASSRLS system role "
        "(SYSTEM_DATABASE_URL / AUTH_DATABASE_URL) in this environment."
    ),
)


def _partition_exists(name: str) -> bool:
    from sqlalchemy import text

    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        return bool(
            session.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{name}"},
            ).scalar()
        )


def _drop_partition(name: str) -> None:
    from sqlalchemy import text

    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        session.execute(text(f"DROP TABLE IF EXISTS {name}"))
        session.commit()


@pytest.fixture()
def seeded_match():
    seeded = seed_workspace_with_variant("partition-create-live")
    competitor_id = seed_competitor(seeded, "Partition Create Live Competitor")
    match_id = seed_match(seeded, competitor_id, "https://partition-create-live.invalid/p/1")
    try:
        yield {"seeded": seeded, "competitor_id": competitor_id, "match_id": match_id}
    finally:
        cleanup_seeded_workspace(seeded)


# --- AS-1/AS-4/SC-001: current+next month created; webhook_events skipped --


def test_current_and_next_month_created_for_existing_tables_absent_table_skipped() -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.partitions import create_missing_partitions, month_partition_bounds
    from app_shared.maintenance.registry import PARTITIONED_TABLES

    now = datetime.now(timezone.utc)

    with get_system_sessionmaker()() as session:
        report = create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    assert "webhook_events" in report.tables_skipped_absent

    for entry in PARTITIONED_TABLES:
        if entry.name == "webhook_events":
            continue
        for offset in (0, 1):
            suffix, _start, _end = month_partition_bounds(now, offset)
            assert _partition_exists(f"{entry.name}_{suffix}")


# --- AS-2/FR-006/SC-002: re-run is a no-op ----------------------------------


def test_rerun_is_a_no_op() -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.partitions import create_missing_partitions

    now = datetime.now(timezone.utc)

    with get_system_sessionmaker()() as session:
        create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    with get_system_sessionmaker()() as session:
        report = create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    assert report.partitions_created == []


# --- AS-3/FR-005: a missing current-month partition is self-healed ---------


def test_missing_current_month_partition_is_self_healed() -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.partitions import create_missing_partitions, month_partition_bounds

    now = datetime.now(timezone.utc)
    suffix, _start, _end = month_partition_bounds(now, 0)
    current_partition = f"price_observations_{suffix}"

    # Ensure it exists first (idempotent baseline), then drop it
    # out-of-band to simulate a gap.
    with get_system_sessionmaker()() as session:
        create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    _drop_partition(current_partition)
    assert not _partition_exists(current_partition)

    with get_system_sessionmaker()() as session:
        report = create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    assert current_partition in report.partitions_created
    assert _partition_exists(current_partition)


# --- A write dated into next month succeeds once its partition exists ------


def test_write_dated_into_next_month_succeeds(seeded_match: dict) -> None:
    from app_shared.database import get_session, get_system_sessionmaker
    from app_shared.maintenance.partitions import create_missing_partitions
    from app_shared.models.observations import PriceObservation

    fixture = seeded_match
    seeded = fixture["seeded"]

    now = datetime.now(timezone.utc)
    with get_system_sessionmaker()() as session:
        create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()

    # Land squarely in next month regardless of today's day-of-month.
    next_month_ts = (now.replace(day=1) + timedelta(days=32)).replace(
        day=1, hour=12, minute=0, second=0, microsecond=0
    )

    with get_session() as session:
        observation = PriceObservation(
            workspace_id=seeded.workspace_id,
            scraped_at=next_month_ts,
            match_id=fixture["match_id"],
            product_id=seeded.product_id,
            product_variant_id=seeded.product_variant_id,
            price=Decimal("9.9900"),
            currency="USD",
            success=True,
            comparable=True,
        )
        session.add(observation)
        session.commit()

    with get_session() as session:
        fetched = session.get(PriceObservation, (observation.id, next_month_ts))
        assert fetched is not None
        assert fetched.workspace_id == seeded.workspace_id
