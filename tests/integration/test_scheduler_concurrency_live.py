"""Live scheduler concurrency + crash-safety + cascade-delete test (SPEC-13
US3 T026, FR-008/014/020/021; `contracts/scheduler-loop.md` "Claim + process"
+ "Ordering & crash safety"; US3 AS-1..4; SC-003; quickstart Scenarios 4/7).

Exercises `app.scheduler.refresh.run_refresh_pass` directly against a real
Postgres (through the BYPASSRLS `get_system_session` seam) and a real Redis
(dispatch enqueue) -- mirrors `test_scheduler_pass_live.py`'s (T023) fixture/
probe idiom, extended to the multi-instance + crash + cascade scenarios
US2's single-pass tests don't cover:

1. Two `run_refresh_pass` passes launched concurrently (a `threading.Barrier`
   synchronizes their start) over one shared due set -> `SELECT ... FOR
   UPDATE SKIP LOCKED` guarantees each due rule is claimed by exactly one of
   them: `fired_1 + fired_2 == len(rules)` (0 duplicate, 0 missed) and
   exactly one `ScrapeJob` per rule (US3 AS-1, SC-003).
2. A per-rule crash **after** `create_scope_job` (enqueue already happened)
   but **before** `commit()` -- simulated by monkeypatching
   `compute_next_run_at` to raise once -- rolls back the *entire* per-rule
   transaction: `next_run_at`/`last_run_at`/`locked_at` stay unchanged and
   the (uncommitted) `ScrapeJob` row never persists. A later pass re-claims
   the still-due rule and fires it exactly once (FR-014, US3 AS-2).
3. Deleting a scope-target row (here: a `competitors` row) cascade-deletes
   its referencing `refresh_rules` row via the composite
   `(workspace_id, competitor_id)` `ondelete="CASCADE"` FK (T006) -- no
   application code touches `refresh_rules` at all. The next pass therefore
   never sees, blocks on, or dereferences the deleted rule/target; a
   sibling rule in the same workspace still fires normally (FR-020, US3
   AS-4, quickstart Scenario 7).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-13 `refresh_rules`
migration applied) AND a reachable Redis (`REDIS_URL`) AND a usable
BYPASSRLS system role (`SYSTEM_DATABASE_URL` or its `AUTH_DATABASE_URL`
fallback). Not runnable in the no-Docker-daemon build environment used to
author this feature -- SKIPS cleanly whenever any of those aren't
reachable/configured or the `refresh_rules` table doesn't exist yet.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
            name=f"live-concurrency-rule-{uuid.uuid4().hex[:8]}",
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


def _fetch_jobs_for_workspace(workspace_id: uuid.UUID) -> list[object]:
    from app_shared.database import get_session
    from app_shared.models.jobs import ScrapeJob
    from app_shared.repository import scoped_select

    with get_session() as session:
        return list(
            session.execute(scoped_select(ScrapeJob, workspace_id)).scalars().all()
        )


def _fetch_rule(rule_id: uuid.UUID):
    from app_shared.database import get_session
    from app_shared.models.refresh_rules import RefreshRule

    with get_session() as session:
        return session.get(RefreshRule, rule_id)


# --- US3 AS-1 / SC-003: two overlapping passes, 0 dup / 0 miss --------------


@pytest.fixture()
def seeded_with_multiple_due_rules():
    seeded = seed_workspace_with_variant("scheduler-concurrency-live")
    competitor_ids = [
        seed_competitor(seeded, f"Concurrency Live Competitor {i}") for i in range(4)
    ]
    for i, competitor_id in enumerate(competitor_ids):
        seed_match(
            seeded, competitor_id, f"https://scheduler-concurrency-live.invalid/p/{i}"
        )
    try:
        yield {"seeded": seeded, "competitor_ids": competitor_ids}
    finally:
        _cleanup(seeded)


def test_two_overlapping_passes_fire_each_rule_exactly_once(
    seeded_with_multiple_due_rules: dict,
) -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.enums import ScrapeScope
    from app.scheduler.refresh import run_refresh_pass

    fixture = seeded_with_multiple_due_rules
    seeded = fixture["seeded"]
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    rule_ids = [
        _seed_refresh_rule(
            seeded,
            scope=ScrapeScope.COMPETITOR,
            competitor_id=competitor_id,
            next_run_at=past,
            interval_minutes=15,
        )
        for competitor_id in fixture["competitor_ids"]
    ]

    run_time = datetime.now(timezone.utc)
    barrier = threading.Barrier(2)

    def _run_pass() -> int:
        barrier.wait(timeout=10)
        return run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_1 = pool.submit(_run_pass)
        future_2 = pool.submit(_run_pass)
        fired_1 = future_1.result(timeout=30)
        fired_2 = future_2.result(timeout=30)

    # SKIP LOCKED -> each due rule claimed by exactly one of the two
    # overlapping passes -- 0 duplicate, 0 missed (US3 AS-1, SC-003).
    assert fired_1 + fired_2 == len(rule_ids)

    jobs = _fetch_jobs_for_workspace(seeded.workspace_id)
    assert len(jobs) == len(rule_ids)  # exactly one SCHEDULED job per rule -- no dup

    for rule_id in rule_ids:
        rule = _fetch_rule(rule_id)
        assert rule.next_run_at > run_time
        assert rule.last_run_at is not None


# --- FR-014, US3 AS-2: crash-before-commit is retry-safe, re-fires once ----


@pytest.fixture()
def seeded_with_one_due_rule():
    seeded = seed_workspace_with_variant("scheduler-crash-live")
    competitor_id = seed_competitor(seeded, "Crash Live Competitor")
    seed_match(seeded, competitor_id, "https://scheduler-crash-live.invalid/p/1")
    try:
        yield {"seeded": seeded, "competitor_id": competitor_id}
    finally:
        _cleanup(seeded)


def test_crash_before_commit_leaves_next_run_at_unchanged_and_later_pass_refires_once(
    seeded_with_one_due_rule: dict,
) -> None:
    from app_shared.database import get_system_sessionmaker
    from app_shared.enums import ScrapeScope
    import app.scheduler.refresh as refresh_module
    from app.scheduler.refresh import run_refresh_pass

    fixture = seeded_with_one_due_rule
    seeded = fixture["seeded"]
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rule_id = _seed_refresh_rule(
        seeded,
        scope=ScrapeScope.COMPETITOR,
        competitor_id=fixture["competitor_id"],
        next_run_at=past,
        interval_minutes=15,
    )

    real_compute_next_run_at = refresh_module.compute_next_run_at

    def _boom(rule: object, run_time: datetime) -> datetime:
        # create_scope_job has already run (enqueue happened) by the time
        # refresh.py calls compute_next_run_at -- raising here simulates a
        # crash after claim+enqueue but before commit (FR-014).
        raise RuntimeError("simulated crash after claim+enqueue, before commit")

    run_time = datetime.now(timezone.utc)
    refresh_module.compute_next_run_at = _boom
    try:
        fired_crash = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)
    finally:
        refresh_module.compute_next_run_at = real_compute_next_run_at

    assert fired_crash == 0

    rule_after_crash = _fetch_rule(rule_id)
    assert rule_after_crash.next_run_at == past  # unchanged -- whole transaction rolled back
    assert rule_after_crash.last_run_at is None
    assert rule_after_crash.locked_at is None

    # The enqueued ScrapeJob row was never committed -- it rolled back with
    # everything else in the same per-rule transaction.
    assert _fetch_jobs_for_workspace(seeded.workspace_id) == []

    fired_retry = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)
    assert fired_retry == 1

    rule_after_retry = _fetch_rule(rule_id)
    assert rule_after_retry.next_run_at > run_time

    jobs_after_retry = _fetch_jobs_for_workspace(seeded.workspace_id)
    assert len(jobs_after_retry) == 1


# --- FR-020, US3 AS-4: cascade-deleted scope target removes the rule -------


def test_cascade_deleted_scope_target_removes_rule_and_pass_continues() -> None:
    from sqlalchemy import text

    from app_shared.database import get_session, get_system_sessionmaker
    from app_shared.enums import ScrapeScope
    from app.scheduler.refresh import run_refresh_pass

    seeded = seed_workspace_with_variant("scheduler-cascade-live")
    competitor_gone_id = seed_competitor(seeded, "Cascade Gone Competitor")
    seed_match(seeded, competitor_gone_id, "https://scheduler-cascade-live.invalid/p/gone")
    competitor_normal_id = seed_competitor(seeded, "Cascade Normal Competitor")
    seed_match(
        seeded, competitor_normal_id, "https://scheduler-cascade-live.invalid/p/normal"
    )

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rule_gone_id = _seed_refresh_rule(
        seeded, scope=ScrapeScope.COMPETITOR, competitor_id=competitor_gone_id, next_run_at=past
    )
    rule_normal_id = _seed_refresh_rule(
        seeded, scope=ScrapeScope.COMPETITOR, competitor_id=competitor_normal_id, next_run_at=past
    )

    try:
        # Delete the competitor row directly -- the composite
        # (workspace_id, competitor_id) FK on refresh_rules is
        # ondelete="CASCADE" (T006), so this alone must cascade-delete
        # rule_gone. No application code touches refresh_rules here.
        with get_session() as session:
            session.execute(
                text("DELETE FROM competitor_product_matches WHERE competitor_id = :cid"),
                {"cid": competitor_gone_id},
            )
            session.execute(text("DELETE FROM competitors WHERE id = :cid"), {"cid": competitor_gone_id})
            session.commit()

        assert _fetch_rule(rule_gone_id) is None  # cascade-deleted by the FK, not app code

        run_time = datetime.now(timezone.utc)
        fired = run_refresh_pass(get_system_sessionmaker(), now=run_time, batch_limit=10)

        # The pass never sees (so never blocks on or dereferences) the
        # deleted rule/target -- only the sibling rule was still there to
        # claim and fires normally.
        assert fired == 1
        rule_normal = _fetch_rule(rule_normal_id)
        assert rule_normal.next_run_at > run_time

        jobs = _fetch_jobs_for_workspace(seeded.workspace_id)
        assert len(jobs) == 1
        assert jobs[0].competitor_id == competitor_normal_id
    finally:
        _cleanup(seeded)
