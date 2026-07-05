"""Live scheduler refresh-pass test (SPEC-13 US2 T023, FR-007/008/009/012/
013/015/016/017; `contracts/scheduler-loop.md`; US2 AS-1..6, SC-002/005/006).

Exercises `app.scheduler.refresh.run_refresh_pass` directly against a
real Postgres (through the BYPASSRLS `get_system_session` seam) and a
real Redis (dispatch enqueue) -- no HTTP layer involved, mirroring
`contracts/scheduler-loop.md`'s claim -> `create_scope_job` -> advance ->
commit loop:

1. A due `COMPETITOR`-scope rule whose competitor has >=1 ACTIVE match
   -> exactly one `ScrapeJob` (`type=SCHEDULED`, `source=SCHEDULER`,
   `scope=COMPETITOR`) with one target per active match, dispatch
   enqueued once, `last_run_at`/`next_run_at` advanced (SC-002).
2. A due rule whose scope resolves to zero ACTIVE matches -> schedule
   still advances, no job/dispatch (FR-015, SC-006).
3. A far-past `next_run_at` (backlog) -> fires once this pass and lands
   strictly in the future -- no per-missed-interval catch-up (FR-016,
   SC-005).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-13 `refresh_rules`
migration applied) AND a reachable Redis (`REDIS_URL`) AND a usable
BYPASSRLS system role (`SYSTEM_DATABASE_URL` or its `AUTH_DATABASE_URL`
fallback). Not runnable in the no-Docker-daemon build environment used
to author this feature -- SKIPS cleanly whenever any of those aren't
reachable/configured or the `refresh_rules` table doesn't exist yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
        "scrape_jobs",
        "scrape_job_targets",
        "refresh_rules",
    }
)


def _live_scheduler_reachable() -> bool:
    """Best-effort probe: Postgres (+ required tables) + Redis + a usable
    BYPASSRLS system session, all reachable."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect, text

        from app_shared.database import (
            check_connection,
            get_engine,
            get_system_sessionmaker,
        )
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
    not _live_scheduler_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, SPEC-13 refresh_rules "
        "migration applied), a reachable Redis (REDIS_URL), and a usable "
        "BYPASSRLS system role (SYSTEM_DATABASE_URL / AUTH_DATABASE_URL) "
        "in this environment."
    ),
)


def _seed_refresh_rule(
    seeded,
    *,
    scope,
    next_run_at: datetime,
    competitor_id: uuid.UUID | None = None,
    interval_minutes: int = 15,
    enabled: bool = True,
):
    from app_shared.database import get_session
    from app_shared.models.refresh_rules import RefreshRule

    with get_session() as session:
        rule = RefreshRule(
            workspace_id=seeded.workspace_id,
            name=f"live-refresh-rule-{uuid.uuid4().hex[:8]}",
            scope=scope,
            competitor_id=competitor_id,
            interval_minutes=interval_minutes,
            enabled=enabled,
            next_run_at=next_run_at,
        )
        session.add(rule)
        session.commit()
        return rule.id


def _cleanup(seeded) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM refresh_rules WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.commit()
    cleanup_seeded_workspace(seeded)


@pytest.fixture()
def seeded_with_competitor_and_active_matches():
    seeded = seed_workspace_with_variant("scheduler-pass-live")
    competitor_id = seed_competitor(seeded, "Scheduler Pass Live Competitor")
    match_ids = [
        seed_match(seeded, competitor_id, f"https://scheduler-pass-live.invalid/p/{i}")
        for i in range(2)
    ]
    try:
        yield {
            "seeded": seeded,
            "competitor_id": competitor_id,
            "match_ids": match_ids,
        }
    finally:
        _cleanup(seeded)


@pytest.fixture()
def seeded_with_competitor_no_matches():
    seeded = seed_workspace_with_variant("scheduler-pass-zero-live")
    competitor_id = seed_competitor(seeded, "Scheduler Pass Zero Live Competitor")
    try:
        yield {"seeded": seeded, "competitor_id": competitor_id}
    finally:
        _cleanup(seeded)


def _fetch_jobs_for_workspace(workspace_id: uuid.UUID) -> list[object]:
    from app_shared.database import get_session
    from app_shared.models.jobs import ScrapeJob
    from app_shared.repository import scoped_select

    with get_session() as session:
        return list(
            session.execute(scoped_select(ScrapeJob, workspace_id)).scalars().all()
        )


def _fetch_targets_for_job(workspace_id: uuid.UUID, job_id: uuid.UUID) -> list[object]:
    from app_shared.database import get_session
    from app_shared.models.jobs import ScrapeJobTarget
    from app_shared.repository import scoped_select

    with get_session() as session:
        return list(
            session.execute(
                scoped_select(ScrapeJobTarget, workspace_id).where(
                    ScrapeJobTarget.scrape_job_id == job_id
                )
            )
            .scalars()
            .all()
        )


def _fetch_rule(rule_id: uuid.UUID):
    from app_shared.database import get_session
    from app_shared.models.refresh_rules import RefreshRule

    with get_session() as session:
        return session.get(RefreshRule, rule_id)


# --- SC-002: due rule w/ active matches fires one SCHEDULED/SCHEDULER job ----


def test_due_rule_with_active_matches_fires_one_job_one_target_per_match(
    seeded_with_competitor_and_active_matches: dict,
) -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.enums import ScrapeJobSource, ScrapeJobStatus, ScrapeJobType, ScrapeScope
    from app.scheduler.refresh import run_refresh_pass

    fixture = seeded_with_competitor_and_active_matches
    seeded = fixture["seeded"]

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rule_id = _seed_refresh_rule(
        seeded,
        scope=ScrapeScope.COMPETITOR,
        competitor_id=fixture["competitor_id"],
        next_run_at=past,
        interval_minutes=15,
    )

    run_time = datetime.now(timezone.utc)
    fired = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)
    assert fired == 1

    jobs = _fetch_jobs_for_workspace(seeded.workspace_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.type == ScrapeJobType.SCHEDULED
    assert job.source == ScrapeJobSource.SCHEDULER
    assert job.scope == ScrapeScope.COMPETITOR
    assert job.status == ScrapeJobStatus.PENDING
    assert job.total_targets == len(fixture["match_ids"])

    targets = _fetch_targets_for_job(seeded.workspace_id, job.id)
    assert {target.match_id for target in targets} == set(fixture["match_ids"])

    rule = _fetch_rule(rule_id)
    assert rule.last_run_at is not None
    assert abs((rule.last_run_at - run_time).total_seconds()) < 5
    assert rule.next_run_at > run_time


# --- FR-015/SC-006: zero-match rule advances schedule, no job/dispatch ------


def test_zero_match_rule_advances_schedule_no_job(
    seeded_with_competitor_no_matches: dict,
) -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.enums import ScrapeScope
    from app.scheduler.refresh import run_refresh_pass

    fixture = seeded_with_competitor_no_matches
    seeded = fixture["seeded"]

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rule_id = _seed_refresh_rule(
        seeded,
        scope=ScrapeScope.COMPETITOR,
        competitor_id=fixture["competitor_id"],
        next_run_at=past,
        interval_minutes=15,
    )

    run_time = datetime.now(timezone.utc)
    fired = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)
    assert fired == 1

    jobs = _fetch_jobs_for_workspace(seeded.workspace_id)
    assert jobs == []

    rule = _fetch_rule(rule_id)
    assert rule.last_run_at is not None
    assert abs((rule.last_run_at - run_time).total_seconds()) < 5
    assert rule.next_run_at > run_time


# --- FR-016/SC-005: far-past next_run_at fires once, lands strictly future --


def test_far_past_next_run_at_fires_once_and_lands_strictly_future(
    seeded_with_competitor_no_matches: dict,
) -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.enums import ScrapeScope
    from app.scheduler.refresh import run_refresh_pass

    fixture = seeded_with_competitor_no_matches
    seeded = fixture["seeded"]

    far_past = datetime.now(timezone.utc) - timedelta(days=30)
    interval_minutes = 5
    rule_id = _seed_refresh_rule(
        seeded,
        scope=ScrapeScope.COMPETITOR,
        competitor_id=fixture["competitor_id"],
        next_run_at=far_past,
        interval_minutes=interval_minutes,
    )

    run_time = datetime.now(timezone.utc)
    fired = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)
    assert fired == 1

    rule = _fetch_rule(rule_id)
    # Backlog fire-once: exactly one interval past `run_time`, never a
    # catch-up chain of many missed intervals since `far_past`.
    assert rule.next_run_at > run_time
    assert rule.next_run_at <= run_time + timedelta(minutes=interval_minutes, seconds=5)

    # A second pass immediately after finds nothing due (already advanced
    # strictly into the future) -- exactly-once, not re-fired.
    fired_again = run_refresh_pass(
        get_system_sessionmaker(), now=run_time, batch_limit=10
    )
    assert fired_again == 0
