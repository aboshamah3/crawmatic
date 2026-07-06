"""Live Postgres+Redis flush/promote test (SPEC-12 US5 T033,
`contracts/stats-buffer.md` §Flush, FR-023/FR-024, SC-003) — ⏸ DEFERRED.

Buffers qualifying attempt stats for one profile via
`app_shared.strategy.stats_buffer.record_attempt`, then exercises
`app_shared.strategy.flush.flush_profile` end-to-end against a real
Postgres + Redis:

1. Buffering 3 qualifying successes across 3 distinct URLs for one
   `(profile, ACCESS, DIRECT_HTTP)` key, then flushing, issues exactly
   one `INSERT ... ON CONFLICT DO UPDATE` (`count = count + delta`) row
   for that key and (combined persisted+pending counts crossing the
   promotion bar in this same pass) promotes the profile to `ACTIVE`
   with `preferred_access_method=DIRECT_HTTP` (AS2/AS3, FR-023/FR-024).
2. A second flush with no new buffered activity in between writes
   nothing (`keys_flushed == 0`, the persisted counters are unchanged) —
   `stratdirty` no longer carries the profile once drained.

Needs a reachable Postgres (`DATABASE_URL`, RLS enforced, SPEC-12
migration applied) AND a reachable Redis (`REDIS_URL`). Not runnable in
the no-Docker-daemon build environment used to author this feature —
SKIPS cleanly whenever either isn't usable or the required tables don't
exist (mirrors `tests/integration/test_promotion_apply.py`'s skip
mechanism, extended with a Redis ping).

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-12 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_reachable() -> bool:
    """Best-effort probe: Postgres (SPEC-12 tables present) + Redis."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine
        from app_shared.redis_client import get_redis_client

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not {"domain_strategy_profiles", "strategy_attempt_stats"} <= table_names:
            return False
        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_reachable(),
    reason="No reachable Postgres (SPEC-12 migration applied) + Redis in this environment",
)


@pytest.fixture()
def seeded_profile() -> Iterator[uuid.UUID]:
    """One workspace + one competitor + one `DISCOVERY_REQUIRED` profile,
    ready for buffered stats + `flush_profile` (mirrors
    `tests/integration/test_promotion_apply.py`'s `seeded_profile`)."""
    from app_shared.database import get_session
    from app_shared.enums import StrategyStatus, WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.models.strategy import DomainStrategyProfile

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"strategy-flush-test {unique}",
            slug=f"strategy-flush-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        competitor = Competitor(
            workspace_id=workspace.id,
            name=f"competitor {unique}",
            domain=f"strategy-flush-{unique}.invalid",
        )
        session.add(competitor)
        session.flush()

        profile = DomainStrategyProfile(
            workspace_id=workspace.id,
            competitor_id=competitor.id,
            domain=f"strategy-flush-{unique}.invalid",
            url_pattern=f"strategy-flush-{unique}.invalid/products/*",
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
            text("DELETE FROM strategy_attempt_stats WHERE domain_strategy_profile_id = :pid"),
            {"pid": profile_id},
        )
        session.execute(
            text("DELETE FROM domain_strategy_profiles WHERE id = :pid"), {"pid": profile_id}
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


def _buffer_qualifying_successes(*, workspace_id: uuid.UUID, profile_id: uuid.UUID, count: int) -> None:
    from app_shared.enums import AccessMethod, MethodType
    from app_shared.redis_client import get_redis_client
    from app_shared.strategy.stats_buffer import record_attempt

    redis = get_redis_client()
    for i in range(count):
        record_attempt(
            redis,
            workspace_id=workspace_id,
            profile_id=profile_id,
            method_type=MethodType.ACCESS,
            method_name=AccessMethod.DIRECT_HTTP.value,
            success=True,
            response_time_ms=120,
            confidence=0.9,
            url=f"https://shop.example.com/products/item-{i}",
            qualifying=True,
            ttl_seconds=3600,
        )


def test_flush_upserts_once_per_key_and_promotes(seeded_profile: uuid.UUID) -> None:
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, MethodType
    from app_shared.models.strategy import DomainStrategyProfile, StrategyAttemptStats
    from app_shared.redis_client import get_redis_client
    from app_shared.strategy.flush import flush_profile

    with get_session() as session:
        workspace_id = session.get(DomainStrategyProfile, seeded_profile).workspace_id

    _buffer_qualifying_successes(workspace_id=workspace_id, profile_id=seeded_profile, count=3)

    redis = get_redis_client()
    with get_session() as session:
        result = flush_profile(session, redis, seeded_profile)
        session.commit()

    assert result.keys_flushed == 1
    assert len(result.transitions) == 1
    transition = result.transitions[0]
    assert transition.new_status.value == "ACTIVE"
    assert transition.change == "PROMOTED"
    assert transition.method == AccessMethod.DIRECT_HTTP.value

    with get_session() as session:
        rows = (
            session.query(StrategyAttemptStats)
            .filter(StrategyAttemptStats.domain_strategy_profile_id == seeded_profile)
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.method_type == MethodType.ACCESS
        assert row.method_name == AccessMethod.DIRECT_HTTP.value
        assert row.attempt_count == 3
        assert row.success_count == 3
        assert row.failure_count == 0

        profile = session.get(DomainStrategyProfile, seeded_profile)
        assert profile.status.value == "ACTIVE"
        assert profile.preferred_access_method == AccessMethod.DIRECT_HTTP

    # `stratdirty` no longer carries the profile once fully drained.
    from app_shared.strategy.stats_buffer import dirty_key

    assert str(seeded_profile) not in redis.smembers(dirty_key(workspace_id))


def test_second_flush_with_no_new_activity_writes_nothing(seeded_profile: uuid.UUID) -> None:
    from app_shared.database import get_session
    from app_shared.models.strategy import DomainStrategyProfile, StrategyAttemptStats
    from app_shared.redis_client import get_redis_client
    from app_shared.strategy.flush import flush_profile

    with get_session() as session:
        workspace_id = session.get(DomainStrategyProfile, seeded_profile).workspace_id

    _buffer_qualifying_successes(workspace_id=workspace_id, profile_id=seeded_profile, count=3)

    redis = get_redis_client()
    with get_session() as session:
        first = flush_profile(session, redis, seeded_profile)
        session.commit()
    assert first.keys_flushed == 1

    with get_session() as session:
        before = (
            session.query(StrategyAttemptStats.attempt_count, StrategyAttemptStats.success_count)
            .filter(StrategyAttemptStats.domain_strategy_profile_id == seeded_profile)
            .one()
        )

    with get_session() as session:
        second = flush_profile(session, redis, seeded_profile)
        session.commit()
    assert second.keys_flushed == 0
    assert second.transitions == ()

    with get_session() as session:
        after = (
            session.query(StrategyAttemptStats.attempt_count, StrategyAttemptStats.success_count)
            .filter(StrategyAttemptStats.domain_strategy_profile_id == seeded_profile)
            .one()
        )

    assert before == after


def test_flush_profile_with_no_pending_activity_is_a_noop(seeded_profile: uuid.UUID) -> None:
    from app_shared.database import get_session
    from app_shared.redis_client import get_redis_client
    from app_shared.strategy.flush import flush_profile

    redis = get_redis_client()
    with get_session() as session:
        result = flush_profile(session, redis, seeded_profile)
        session.commit()

    assert result.keys_flushed == 0
    assert result.transitions == ()
