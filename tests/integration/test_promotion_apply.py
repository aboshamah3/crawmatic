"""Live-Postgres promotion-apply test (SPEC-12 US1 T018, `contracts/promotion.md`,
FR-010/FR-011, SC-001) — ⏸ DEFERRED.

Seeds a `domain_strategy_profiles` row plus qualifying
`strategy_attempt_stats` directly (US1 is testable with direct writes,
per tasks.md), then exercises `app_shared.strategy.promotion.apply_promotion`
against the real guarded `UPDATE ... WHERE id=:pid AND status IN (...)
AND (preferred_* IS NULL OR preferred_* <> :m)` statement:

1. A qualifying sequence (>=3 qualifying successes, confidence, access
   method) -> `apply_promotion` returns `True`, `preferred_access_method`/
   `access_confidence`/`confirmed_success_count`/`status=ACTIVE` are set.
2. A non-qualifying decision (`promote=False`) -> `apply_promotion` is a
   no-op; the profile stays un-promoted.
3. A second concurrent `apply_promotion` call for the *same* method after
   the first already won -> `rowcount == 0` -> returns `False` and does
   NOT double-increment `confirmed_success_count` (Edge Cases "Concurrent
   promotion").

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-12 migration already applied (`alembic
upgrade head`). Not runnable in the no-Docker-daemon build environment
used to author this feature — SKIPS cleanly whenever `Settings`/
`DATABASE_URL` isn't usable, a real connection attempt fails, or the
`domain_strategy_profiles` table doesn't exist yet (mirrors
`tests/integration/test_scrape_profiles_crud_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-12 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest


def _live_strategy_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-12 `domain_strategy_profiles` table already exists (migration
    applied)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if "domain_strategy_profiles" not in table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_strategy_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-12 "
        "domain_strategy_profiles migration applied in this environment"
    ),
)


@pytest.fixture()
def seeded_profile() -> Iterator[uuid.UUID]:
    """One workspace + one competitor + one `DISCOVERY_REQUIRED` profile,
    ready for `apply_promotion`."""
    from app_shared.database import get_session
    from app_shared.enums import StrategyStatus, WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.models.strategy import DomainStrategyProfile

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"strategy-promotion-test {unique}",
            slug=f"strategy-promotion-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        competitor = Competitor(
            workspace_id=workspace.id,
            name=f"competitor {unique}",
            domain=f"strategy-promotion-{unique}.invalid",
        )
        session.add(competitor)
        session.flush()

        profile = DomainStrategyProfile(
            workspace_id=workspace.id,
            competitor_id=competitor.id,
            domain=f"strategy-promotion-{unique}.invalid",
            url_pattern=f"strategy-promotion-{unique}.invalid/products/*",
            url_pattern_version=1,
            status=StrategyStatus.DISCOVERY_REQUIRED,
        )
        session.add(profile)
        session.flush()
        session.commit()

        profile_id, workspace_id = profile.id, workspace.id

    yield profile_id

    from sqlalchemy import text

    with get_session() as session:
        session.execute(
            text("DELETE FROM domain_strategy_profiles WHERE id = :pid"), {"pid": profile_id}
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


def test_qualifying_sequence_promotes_access_method(seeded_profile: uuid.UUID) -> None:
    """US1 AS1: >=3 qualifying successes across >=3 distinct URLs ->
    `apply_promotion` sets preferred method + confidence, bumps
    `confirmed_success_count`, and moves the profile to `ACTIVE`."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, MethodType
    from app_shared.models.strategy import DomainStrategyProfile
    from app_shared.strategy.promotion import (
        MethodStats,
        PromotionThresholds,
        apply_promotion,
        evaluate_promotion,
    )

    thresholds = PromotionThresholds(
        min_successes=3, min_distinct_urls=3, confidence_threshold=Decimal("0.85")
    )
    combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=3, thresholds=thresholds)
    assert decision.promote is True

    with get_session() as session:
        applied = apply_promotion(
            session,
            seeded_profile,
            method_type=MethodType.ACCESS,
            method_name=AccessMethod.PROXY_HTTP.value,
            decision=decision,
        )
        session.commit()
        assert applied is True

    with get_session() as session:
        profile = session.get(DomainStrategyProfile, seeded_profile)
        assert profile is not None
        assert profile.preferred_access_method == AccessMethod.PROXY_HTTP
        assert profile.access_confidence == Decimal("0.9000")
        assert profile.confirmed_success_count == 1
        assert profile.status.value == "ACTIVE"


def test_non_qualifying_decision_leaves_profile_unpromoted(seeded_profile: uuid.UUID) -> None:
    """A non-qualifying sequence (`promote=False`) never applies -- the
    profile stays `DISCOVERY_REQUIRED` with no preferred method set."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, MethodType
    from app_shared.models.strategy import DomainStrategyProfile
    from app_shared.strategy.promotion import (
        MethodStats,
        PromotionThresholds,
        apply_promotion,
        evaluate_promotion,
    )

    thresholds = PromotionThresholds(
        min_successes=3, min_distinct_urls=3, confidence_threshold=Decimal("0.85")
    )
    combined = MethodStats(qualifying_success_count=2, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=2, thresholds=thresholds)
    assert decision.promote is False

    with get_session() as session:
        applied = apply_promotion(
            session,
            seeded_profile,
            method_type=MethodType.ACCESS,
            method_name=AccessMethod.PROXY_HTTP.value,
            decision=decision,
        )
        session.commit()
        assert applied is False

    with get_session() as session:
        profile = session.get(DomainStrategyProfile, seeded_profile)
        assert profile is not None
        assert profile.preferred_access_method is None
        assert profile.status.value == "DISCOVERY_REQUIRED"


def test_second_concurrent_apply_does_not_double_promote(seeded_profile: uuid.UUID) -> None:
    """Edge Cases "Concurrent promotion": once a method has already won
    (preferred_access_method set to :m), a second apply for the *same*
    method_name is a no-op (`rowcount == 0`) -- `confirmed_success_count`
    is never double-incremented."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, MethodType
    from app_shared.models.strategy import DomainStrategyProfile
    from app_shared.strategy.promotion import (
        MethodStats,
        PromotionThresholds,
        apply_promotion,
        evaluate_promotion,
    )

    thresholds = PromotionThresholds(
        min_successes=3, min_distinct_urls=3, confidence_threshold=Decimal("0.85")
    )
    combined = MethodStats(qualifying_success_count=3, confidence=Decimal("0.9"))
    decision = evaluate_promotion(combined, distinct_url_count=3, thresholds=thresholds)
    assert decision.promote is True

    with get_session() as session:
        first = apply_promotion(
            session,
            seeded_profile,
            method_type=MethodType.ACCESS,
            method_name=AccessMethod.PROXY_HTTP.value,
            decision=decision,
        )
        session.commit()
        assert first is True

    # Simulates a second worker flushing the same profile concurrently
    # with the same winning method -- must not win the race twice.
    with get_session() as session:
        second = apply_promotion(
            session,
            seeded_profile,
            method_type=MethodType.ACCESS,
            method_name=AccessMethod.PROXY_HTTP.value,
            decision=decision,
        )
        session.commit()
        assert second is False

    with get_session() as session:
        profile = session.get(DomainStrategyProfile, seeded_profile)
        assert profile is not None
        assert profile.confirmed_success_count == 1
