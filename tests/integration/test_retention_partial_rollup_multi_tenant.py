"""Integration test: retention's verify-before-drop gate over multiple
tenants and variants (EPA C8, F13) — `contracts/retention-drop.md`,
`docs/RETENTION_POLICY.md` §2.3.

Exercises `app_shared.maintenance.retention.run_retention`'s TWO gates
end to end against a real Postgres, across two workspaces and three
product variants sharing one expired `price_observations` partition:

1. Seed workspace A (two variants) and workspace B (one variant), one
   observation each on the same expired UTC date, all in the same
   monthly partition.
2. Roll up only ONE of the three (workspace, variant, date) keys —
   `variant_price_daily_rollups` gets exactly one row. Gate 1 (per-key
   coverage, `retention.py::_rollups_cover_stmt`) sees the other two
   keys missing: the partition is RETAINED and reported
   `partitions_skipped_pending_rollups`.
3. Roll up the remaining two keys. Gate 1 now passes (every key that had
   an observation has a covering rollup row) but the `rollup_completion`
   watermark for the date (C7's table) is still absent — gate 2 fails,
   and the partition is STILL retained. This is the scenario gate 1
   alone cannot see: a batch loop that reached every pair before being
   killed produces per-key-correct rows without ever writing
   `complete = true`.
4. Mark `rollup_completion.complete = true` for the date. Both gates now
   pass and the partition becomes eligible for `DROP TABLE`.

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-07/09/15 tables
migrated) with the EPA C7 `rollup_completion` migration (`b3f0c95a7d21`)
applied, AND a usable BYPASSRLS system role (`SYSTEM_DATABASE_URL` /
`AUTH_DATABASE_URL` fallback). SKIPS cleanly whenever any of that isn't
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
        "variant_price_daily_rollups",
        "rollup_completion",
    }
)


def _retention_partial_rollup_reachable() -> bool:
    """Best-effort probe: Postgres (+ required tables, INCLUDING the C7
    `rollup_completion` migration) and a usable BYPASSRLS system
    session, both reachable."""
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
    not _retention_partial_rollup_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-07/09/15 "
        "tables migrated) with the EPA C7 rollup_completion migration "
        "(b3f0c95a7d21) applied, and a usable BYPASSRLS system role "
        "(SYSTEM_DATABASE_URL / AUTH_DATABASE_URL) in this environment."
    ),
)


# --- raw partition helpers (arbitrary past month, mirrors test_retention_drop_live.py) --


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _partition_name(parent: str, year: int, month: int) -> str:
    return f"{parent}_{year:04d}_{month:02d}"


def _create_month_partition(parent: str, year: int, month: int) -> str:
    from app_shared.database import get_system_sessionmaker

    start, end = _month_bounds(year, month)
    name = _partition_name(parent, year, month)
    from app_shared.maintenance.partitions import (
        _seam_create_stmt,
        partition_seam_available,
    )

    with get_system_sessionmaker()() as session:
        # Built through the SAME seam production uses
        # (`provision_db_roles.sql` §9, added by EPA C9/F14), not raw DDL.
        # On a correctly provisioned database this session is
        # `crawmatic_auth`, which does not OWN the parent and therefore
        # cannot `CREATE TABLE ... PARTITION OF` at all — and a partition
        # created by raw DDL under a privilege workaround ends up owned by
        # the wrong role, so retention's own drop then fails too. Building
        # the fixture the way production builds it keeps the test honest
        # about both.
        if partition_seam_available(session):
            session.execute(
                _seam_create_stmt(
                    name,
                    parent,
                    datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
                    datetime(end.year, end.month, end.day, tzinfo=timezone.utc),
                )
            )
        else:
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
    """Teardown, through the production seam for the same reason the
    fixture creation is (EPA C9/F14): a partition correctly owned by
    `crawmatic_migrate` cannot be reclaimed by a raw statement on the
    `crawmatic_auth` session, so raw teardown would fail on exactly the
    databases that are provisioned properly."""
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.partitions import drop_partition

    with get_system_sessionmaker()() as session:
        drop_partition(session, name)
        session.commit()


def _run_retention(now_utc: datetime):
    from app_shared.config import get_settings
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.retention import run_retention

    # EPA C9 (F14) made `RETENTION_ENABLED_CLASSES` an owner-ratification
    # switch that ships EMPTY, so a default `Settings` now retains
    # everything. This file is about the verify-before-drop GATES, not
    # about the switch, so it enables the one class it exercises —
    # exactly what an owner does after signing
    # `docs/RETENTION_POLICY.md` §2.3. `model_copy` rather than env
    # mutation: `get_settings()` is a cached process-wide singleton.
    settings = get_settings().model_copy(
        update={"RETENTION_ENABLED_CLASSES": ["price_observations"]}
    )
    with get_system_sessionmaker()() as session:
        report = run_retention(session, now_utc=now_utc, settings=settings)
        session.commit()
    return report


# --- seeding: two workspaces, three variants --------------------------------


def _add_variant(seeded: SeededWorkspace) -> uuid.UUID:
    """Seed one additional `product_variants` row under `seeded`'s
    existing product. `seed_workspace_with_variant` only ever creates
    one variant; workspace A in this test needs a second. Cleaned up by
    `cleanup_seeded_workspace` (deletes every variant by `workspace_id`,
    not just the one it returned)."""
    from app_shared.database import get_session
    from app_shared.enums import VariantStatus
    from app_shared.models.catalog import ProductVariant

    with get_session() as session:
        variant = ProductVariant(
            workspace_id=seeded.workspace_id,
            product_id=seeded.product_id,
            title=f"Extra {uuid.uuid4().hex[:8]}",
            current_price=Decimal("0.0000"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add(variant)
        session.commit()
        return variant.id


def _insert_observation(
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    *,
    scraped_at: datetime,
) -> None:
    from app_shared.database import get_session
    from app_shared.models.observations import PriceObservation

    with get_session() as session:
        session.add(
            PriceObservation(
                workspace_id=workspace_id,
                scraped_at=scraped_at,
                match_id=uuid.uuid4(),
                product_id=product_id,
                product_variant_id=variant_id,
                price=Decimal("10.0000"),
                currency="USD",
                success=True,
                comparable=True,
            )
        )
        session.commit()


def _insert_rollup_row(
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    *,
    rollup_date: date,
) -> None:
    from app_shared.database import get_session
    from app_shared.enums import AlertType
    from app_shared.models.rollups import VariantPriceDailyRollup

    with get_session() as session:
        session.add(
            VariantPriceDailyRollup(
                workspace_id=workspace_id,
                product_id=product_id,
                product_variant_id=variant_id,
                date=rollup_date,
                currency="USD",
                client_price=Decimal("10.0000"),
                comparable_competitor_count=0,
                # NOT NULL, no default -- zero comparable competitors is
                # exactly the `NO_COMPETITOR_DATA` classification.
                latest_alert_type=AlertType.NO_COMPETITOR_DATA,
            )
        )
        session.commit()


def _set_completion(day: date, *, complete: bool) -> None:
    """Directly drive the `rollup_completion` watermark (C7's
    `upsert_completion`) for `day`, independent of a real
    `run_daily_rollup` invocation — isolates gate 2 from gate 1 so this
    test can prove they are each independently necessary."""
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.rollup_sql import upsert_completion

    with get_system_sessionmaker()() as session:
        upsert_completion(
            session,
            day,
            last_workspace_id=None,
            last_variant_id=None,
            complete=complete,
            now=datetime.now(timezone.utc),
        )
        session.commit()


def _complete_every_day_in_month(year: int, month: int) -> None:
    """Mark every UTC calendar date in `year`-`month` complete.

    Gate 2 (`retention.py::_completion_watermark_gap_stmt`) checks EVERY
    date in the partition's whole `[d0, d_n)` month range, not just days
    that happened to have an observation -- production's daily-rollup
    scheduler is expected to invoke (and checkpoint) every UTC day
    regardless of whether it turns out to have zero observations, so an
    absent watermark for a quiet day is itself evidence the scheduler
    did not run. This test only puts data on ONE day of the month, so
    making the whole partition eligible means completing the whole
    month, exactly as a real scheduler running daily would have.
    """
    start, end = _month_bounds(year, month)
    day = start
    while day < end:
        _set_completion(day, complete=True)
        day += timedelta(days=1)


def _delete_rollups(workspace_id: uuid.UUID) -> None:
    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM variant_price_daily_rollups WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.commit()


def _delete_completion_month(year: int, month: int) -> None:
    from app_shared.database import get_system_sessionmaker

    start, end = _month_bounds(year, month)
    with get_system_sessionmaker()() as session:
        session.execute(
            text("DELETE FROM rollup_completion WHERE date >= :d0 AND date < :d_n"),
            {"d0": start, "d_n": end},
        )
        session.commit()


# --- F13: per-key coverage AND the completion watermark, both required -----


def test_partial_rollup_across_two_workspaces_three_variants_gates_the_drop() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    year, month = old.year, old.month
    partition = _create_month_partition("price_observations", year, month)
    obs_date = date(year, month, 15)
    obs_ts = datetime(year, month, 15, 12, 0, tzinfo=timezone.utc)

    ws_a = seed_workspace_with_variant("retention-partial-a")
    ws_a_variant2 = _add_variant(ws_a)
    ws_b = seed_workspace_with_variant("retention-partial-b")

    try:
        # Three (workspace, variant) keys, two workspaces, all observed
        # on the same expired UTC day inside the one partition.
        _insert_observation(
            ws_a.workspace_id, ws_a.product_id, ws_a.product_variant_id, scraped_at=obs_ts
        )
        _insert_observation(ws_a.workspace_id, ws_a.product_id, ws_a_variant2, scraped_at=obs_ts)
        _insert_observation(
            ws_b.workspace_id, ws_b.product_id, ws_b.product_variant_id, scraped_at=obs_ts
        )

        # --- Step 1: roll up only ONE of the three keys ---------------------
        _insert_rollup_row(
            ws_a.workspace_id, ws_a.product_id, ws_a.product_variant_id, rollup_date=obs_date
        )

        report = _run_retention(datetime.now(timezone.utc))
        assert partition in report.partitions_skipped_pending_rollups
        assert partition not in report.partitions_dropped
        assert _partition_exists(partition)

        # --- Step 2: roll up the remaining two keys -- gate 1 (per-key
        # coverage) is now fully satisfied, but the rollup_completion
        # watermark is still entirely absent for the month -- gate 2 must
        # still retain the partition (proves gate 1 alone is not
        # sufficient).
        _insert_rollup_row(
            ws_a.workspace_id, ws_a.product_id, ws_a_variant2, rollup_date=obs_date
        )
        _insert_rollup_row(
            ws_b.workspace_id, ws_b.product_id, ws_b.product_variant_id, rollup_date=obs_date
        )

        report = _run_retention(datetime.now(timezone.utc))
        assert partition in report.partitions_skipped_pending_rollups
        assert partition not in report.partitions_dropped
        assert _partition_exists(partition)

        # --- Step 3: complete the watermark for every day of the month --
        # (a real scheduler checkpoints every UTC day, not just days with
        # data) -- both gates now pass.
        _complete_every_day_in_month(year, month)

        report = _run_retention(datetime.now(timezone.utc))
        assert partition in report.partitions_dropped
        assert partition not in report.partitions_skipped_pending_rollups
        assert not _partition_exists(partition)
    finally:
        _delete_completion_month(year, month)
        _delete_rollups(ws_a.workspace_id)
        _delete_rollups(ws_b.workspace_id)
        cleanup_seeded_workspace(ws_a)
        cleanup_seeded_workspace(ws_b)
        _drop_partition_raw(partition)  # no-op if already dropped by step 3
