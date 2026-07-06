"""Live soft-reference tolerance test (SPEC-15 US4 T034, contracts/
soft-reference-tolerance.md, research R10; US4 AS-1/AS-2, SC-007).

Exercises the tolerance guarantee end-to-end against a real Postgres:

1. AS-1 — a `match_current_prices` row whose `observation_id` points at
   a `price_observations.id` that does not exist (equivalent, for every
   reader's purposes, to one whose winning observation's month
   partition has since been dropped by retention — no live reader ever
   re-derives that state from the partition itself) still loads and
   returns correct denormalized data through the one live reader,
   `app.workers.tasks_analysis._load_competitor_rows`
   (SPEC-15 T031 audit finding) — no error/500/row-drop.
2. AS-2 — an explicit fetch of the missing raw `price_observations` row
   is an expected `None` (no exception, FR-021), and
   `app_shared.maintenance.soft_refs.count_tolerated_dangling_refs`
   reports the dangling ref as tolerated/expected, never corruption
   (FR-022).

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-07/15 tables
migrated) AND a usable BYPASSRLS system role (`SYSTEM_DATABASE_URL` /
`AUTH_DATABASE_URL` fallback, for the cross-tenant tolerance-count
probe). SKIPS cleanly whenever either isn't reachable/configured in
this build environment (no live Postgres here — never faked).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

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
        "match_current_prices",
        "price_observations",
    }
)


def _soft_ref_tolerance_live_reachable() -> bool:
    """Best-effort probe: Postgres (+ required tables) and a usable
    BYPASSRLS system session, both reachable (mirrors
    `test_retention_drop_live.py`'s reachability probe)."""
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
    not _soft_ref_tolerance_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-07/15 tables "
        "migrated) and a usable BYPASSRLS system role (SYSTEM_DATABASE_URL "
        "/ AUTH_DATABASE_URL) in this environment."
    ),
)


def _insert_dangling_match_current_price(seeded, competitor_id, match_id) -> uuid.UUID:
    """Insert one `match_current_prices` row whose `observation_id` never
    resolves to a real `price_observations` row -- indistinguishable, to
    every reader, from one whose winning observation's partition has
    since been dropped by retention (contract soft-reference-
    tolerance.md; no live reader ever dereferences `observation_id`)."""
    from app_shared.database import get_session
    from app_shared.models.observations import MatchCurrentPrice

    dangling_observation_id = uuid.uuid4()
    with get_session() as session:
        session.add(
            MatchCurrentPrice(
                workspace_id=seeded.workspace_id,
                match_id=match_id,
                product_id=seeded.product_id,
                product_variant_id=seeded.product_variant_id,
                competitor_id=competitor_id,
                price=Decimal("9.9900"),
                currency="USD",
                comparable=True,
                observation_id=dangling_observation_id,
                success=True,
                scraped_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return dangling_observation_id


# --- AS-1/FR-021: the one live reader tolerates the dangling ref -----------


def test_reader_loads_denormalized_data_despite_dangling_observation_id() -> None:
    from app.workers.tasks_analysis import _load_competitor_rows
    from app_shared.database import get_session

    seeded = seed_workspace_with_variant("soft-ref-tolerance-reader")
    competitor_id = seed_competitor(seeded, "soft-ref-competitor-reader")
    match_id = seed_match(seeded, competitor_id, "https://example.invalid/soft-ref-reader")
    try:
        _insert_dangling_match_current_price(seeded, competitor_id, match_id)

        with get_session() as session:
            rows = _load_competitor_rows(session, seeded.workspace_id, seeded.product_variant_id)

        # No error/500/row-drop -- the row loads with correct denormalized
        # data despite its observation_id never resolving.
        assert len(rows) == 1
        row = rows[0]
        assert row.match_id == match_id
        assert row.price == Decimal("9.9900")
        assert row.currency == "USD"
        assert row.success is True
        assert row.comparable is True
    finally:
        cleanup_seeded_workspace(seeded)


# --- AS-2/FR-021/FR-022: explicit fetch is expected None; check tolerates --


def test_explicit_fetch_of_dangling_observation_is_expected_none_and_tolerated() -> None:
    from app_shared.database import get_session, get_system_sessionmaker
    from app_shared.maintenance.soft_refs import count_tolerated_dangling_refs
    from app_shared.models.observations import PriceObservation
    from app_shared.repository import scoped_select

    seeded = seed_workspace_with_variant("soft-ref-tolerance-count")
    competitor_id = seed_competitor(seeded, "soft-ref-competitor-count")
    match_id = seed_match(seeded, competitor_id, "https://example.invalid/soft-ref-count")
    try:
        dangling_observation_id = _insert_dangling_match_current_price(
            seeded, competitor_id, match_id
        )

        with get_session() as session:
            fetched = (
                session.execute(
                    scoped_select(PriceObservation, seeded.workspace_id).where(
                        PriceObservation.id == dangling_observation_id
                    )
                )
                .scalars()
                .one_or_none()
            )
        # Expected not-found -- no exception (FR-021).
        assert fetched is None

        with get_system_sessionmaker()() as session:
            tolerated = count_tolerated_dangling_refs(session)
        # Informational/expected, never treated as corruption (FR-022).
        assert tolerated >= 1
    finally:
        cleanup_seeded_workspace(seeded)
