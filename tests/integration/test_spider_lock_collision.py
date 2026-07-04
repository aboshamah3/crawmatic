"""Live SPEC-11 US2 match-lock collision integration test (T017,
`contracts/match-lock.md`, `contracts/spider-integration.md` step 3,
SC-002, US2 AS1) — ⏸ DEFERRED.

Pre-holds ``lock:scrape:{workspace_id}:{match_id}`` directly against a
real Redis (simulating "another attempt for this match is already in
flight"), then drives a real ``generic_price_spider`` run for that same
match (via ``run_generic_price_spider_subprocess``, the same
full-reactor-crawl harness `test_spider_batch_live.py` uses). Because the
lock is already held, `_dispatch` (T022) must:

1. Never fetch the target URL at all — the persisted attempt's
   ``status_code`` stays ``NULL`` (no HTTP response was ever obtained;
   the seeded URL is a non-resolvable ``.invalid`` domain precisely so a
   real fetch attempt would be unmistakable).
2. Persist exactly one ``price_observations``/``request_attempts`` row
   for the match, ``success=false``, ``error_code=LOCKED_ALREADY_RUNNING``.
3. Release the semaphore slot taken in step 2 of the dispatch gate (the
   sorted-set semaphore key is empty afterward — no leaked slot).
4. Leave the pre-held lock's token untouched (`SET ... NX` never
   overwrites an existing key, and the spider's own lock acquisition
   correctly returned "already held" rather than stealing it).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-11 migration-free —
only the SPEC-08/10 tables) AND a reachable Redis (`REDIS_URL`) with a
live Scrapyd-equivalent crawl execution (this test runs the real spider
in its own OS process exactly the way Scrapyd would launch one job per
process — see `_scrapyd_spider_live_support.run_generic_price_spider_subprocess`).
Not runnable in the no-Docker-daemon build environment used to author
this feature — SKIPS cleanly whenever Postgres/Redis aren't usable or
the required tables don't exist.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-08/10 migrations applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
    seed_competitor,
    seed_match,
    seed_scrape_profile,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = (
    "competitor_product_matches",
    "competitors",
    "access_policies",
    "scrape_jobs",
    "scrape_job_targets",
    "price_observations",
    "request_attempts",
)

pytestmark = pytest.mark.skipif(
    not live_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-08/10 migrations "
        "applied -- not available in this environment."
    ),
)


def _create_access_policy(workspace_id: uuid.UUID) -> uuid.UUID:
    """Seed a ``default``-named (``WORKSPACE_DEFAULT_POLICY_NAME``) policy
    so `load_targets` resolves a non-``None`` `AccessPolicy` -- without
    one, `_prepare_dispatch` short-circuits to the silent NONE_RESOLVED
    skip and never reaches the SPEC-11 limiter/lock gate at all (mirrors
    `test_spider_access.py::_create_access_policy`)."""
    from app_shared.database import get_session
    from app_shared.enums import AccessStrategy
    from app_shared.models.access import AccessPolicy

    with get_session() as session:
        policy = AccessPolicy(
            workspace_id=workspace_id,
            name="default",
            strategy=AccessStrategy.DIRECT_ONLY,
            max_retries=0,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=False,
        )
        session.add(policy)
        session.commit()
        return policy.id


def _seed_job_target(*, workspace_id: uuid.UUID, match_id: uuid.UUID) -> uuid.UUID:
    """Seed one ``ScrapeJob`` + its single PENDING ``ScrapeJobTarget`` row
    for `match_id` -- the row `mark_target` (via `_flush_batch`) needs to
    resolve in order to transition it (mirrors
    `test_jobs_counters_finalize_live.py::_seed_job_with_targets`, sized
    to exactly one target)."""
    from app_shared.database import get_session
    from app_shared.enums import ScrapeJobSource, ScrapeJobStatus, ScrapeJobType, ScrapeScope, ScrapeTargetStatus
    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

    now = datetime.now(UTC)
    with get_session() as session:
        job = ScrapeJob(
            workspace_id=workspace_id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.RUNNING,
            source=ScrapeJobSource.API,
            total_targets=1,
            started_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()
        session.add(
            ScrapeJobTarget(
                workspace_id=workspace_id,
                scrape_job_id=job.id,
                match_id=match_id,
                status=ScrapeTargetStatus.PENDING,
                created_at=now,
            )
        )
        session.commit()
        return job.id


def _cleanup_access_and_job_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM request_attempts WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM price_observations WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


@dataclass
class _SeededTarget:
    workspace: SeededWorkspace
    match_id: uuid.UUID
    scrape_job_id: uuid.UUID


@pytest.fixture()
def seeded_target() -> Iterator[_SeededTarget]:
    workspace = seed_workspace_with_variant("spec11-lock-collision")
    _create_access_policy(workspace.workspace_id)
    competitor_id = seed_competitor(workspace, "lock-collision-competitor")
    profile_id = seed_scrape_profile(workspace, "lock-collision-profile")
    unique = uuid.uuid4().hex[:8]
    match_id = seed_match(
        workspace,
        competitor_id,
        f"https://spider-lock-collision-{unique}.invalid/product/1",
        scrape_profile_id=profile_id,
    )
    scrape_job_id = _seed_job_target(workspace_id=workspace.workspace_id, match_id=match_id)
    try:
        yield _SeededTarget(workspace=workspace, match_id=match_id, scrape_job_id=scrape_job_id)
    finally:
        _cleanup_access_and_job_rows(workspace.workspace_id)
        cleanup_seeded_workspace(workspace)


def test_lock_collision_skips_no_fetch_and_releases_semaphore(seeded_target: _SeededTarget) -> None:
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod
    from app_shared.limiter.keys import match_lock_key, semaphore_key
    from app_shared.limiter.locks import acquire_match_lock, release_match_lock
    from app_shared.redis_client import get_redis_client

    from ._scrapyd_spider_live_support import run_generic_price_spider_subprocess

    workspace_id = seeded_target.workspace.workspace_id
    match_id = seeded_target.match_id

    redis = get_redis_client()
    lock_key = match_lock_key(workspace_id, match_id)
    owner_token = f"pre-held-owner-{uuid.uuid4().hex}"
    # Simulate "another attempt for this match is already in flight" --
    # pre-hold the lock directly against real Redis, exactly the shape
    # the spider's own acquire would produce.
    assert acquire_match_lock(redis, key=lock_key, token=owner_token, ttl_seconds=600) is True

    try:
        result = run_generic_price_spider_subprocess(
            workspace_id=workspace_id,
            scrape_job_id=seeded_target.scrape_job_id,
            match_ids=[match_id],
        )
        assert result.returncode == 0, result.stderr

        with get_session() as session:
            from sqlalchemy import text

            observation = session.execute(
                text(
                    "SELECT success, error_code, status_code FROM price_observations "
                    "WHERE workspace_id = :ws AND match_id = :match_id"
                ),
                {"ws": workspace_id, "match_id": match_id},
            ).mappings().all()
            assert len(observation) == 1, observation
            assert observation[0]["success"] is False
            assert observation[0]["error_code"] == "LOCKED_ALREADY_RUNNING"
            assert observation[0]["status_code"] is None  # never fetched

            attempts = session.execute(
                text(
                    "SELECT success, error_code, status_code FROM request_attempts "
                    "WHERE workspace_id = :ws AND match_id = :match_id"
                ),
                {"ws": workspace_id, "match_id": match_id},
            ).mappings().all()
            assert len(attempts) == 1, attempts
            assert attempts[0]["success"] is False
            assert attempts[0]["status_code"] is None  # never fetched

            target_row = session.execute(
                text(
                    "SELECT status FROM scrape_job_targets "
                    "WHERE workspace_id = :ws AND scrape_job_id = :job AND match_id = :match_id"
                ),
                {"ws": workspace_id, "job": seeded_target.scrape_job_id, "match_id": match_id},
            ).mappings().one()
            assert target_row["status"] == "SKIPPED"

        # The semaphore slot taken in step 2 of `_dispatch` was released
        # once the lock (step 3) came back denied -- no leaked slot.
        sem_key = semaphore_key(workspace_id, _domain_for(workspace_id, match_id), AccessMethod.DIRECT_HTTP)
        assert redis.zcard(sem_key) == 0

        # The pre-held lock is untouched -- the spider's own acquire
        # correctly reported "already held" without ever stealing it.
        assert redis.get(lock_key) is not None
    finally:
        release_match_lock(redis, key=lock_key, token=owner_token)


def _domain_for(workspace_id: uuid.UUID, match_id: uuid.UUID) -> str:
    """Look up the seeded competitor's domain for `match_id` (needed to
    reconstruct the semaphore key the spider used -- the domain is
    randomized per test run by `seed_competitor`)."""
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        row = session.execute(
            text(
                "SELECT c.domain FROM competitors c "
                "JOIN competitor_product_matches m ON m.competitor_id = c.id "
                "WHERE m.workspace_id = :ws AND m.id = :match_id"
            ),
            {"ws": workspace_id, "match_id": match_id},
        ).mappings().one()
        return row["domain"]
