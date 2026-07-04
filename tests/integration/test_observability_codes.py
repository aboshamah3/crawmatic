"""Live SPEC-11 US4 error-code attribution integration test (T029,
`contracts/observability.md`, SC-006, US4 AS1/AS2) — ⏸ DEFERRED.

Drives the two documented outcomes straight through the **exact
production functions** that persist them -- never a hand-rolled
duplicate write -- and asserts the persisted ``scrape_job_targets`` row
for each:

1. **Lock collision**: builds a :class:`~scrape_core.items.ScrapeResult`
   exactly the shape `generic_price_spider._dispatch` (T022) emits on a
   held match lock (``success=False``, ``error_code=
   LOCKED_ALREADY_RUNNING``, no ``match_lock_token`` -- a collision never
   acquired a lock) and feeds it through
   ``scrape_core.pipelines._flush_batch`` -- the SAME batched-persistence
   writer every real crawl uses (T023) -- asserting the target lands
   ``SKIPPED`` + ``LOCKED_ALREADY_RUNNING`` (relies on the T026
   ``mark_target`` error_code broadening: this status is not ``FAILED``).
2. **Rate-limit overflow**: calls
   ``price_monitor.spiders.generic_price_spider._mark_target_deferred_rate_limited``
   -- the exact helper the T027 requeue-cap-overflow branch calls (never
   through ``ScrapeResult``, since an overflowed target never dispatched
   a request at all) -- asserting the target lands ``DEFERRED`` +
   ``RATE_LIMITED`` (same T026 broadening; also non-terminal, per
   ``app_shared.jobs.targets._TERMINAL_TARGET_STATUSES``).

Both routes converge on the single ``mark_target`` writer
(`contracts/observability.md` "Error-code mapping") -- this test is the
SC-006 "100% attributable" proof: distinct codes, distinct statuses,
same writer.

Needs a reachable Postgres (`DATABASE_URL`, SPEC-11 migration-free --
only the SPEC-08/10 tables) AND a reachable Redis (`REDIS_URL` --
``_flush_batch`` also enqueues ``SCRAPE_FINALIZE_JOBS`` via the Celery
producer, which needs a broker connection to publish even though this
test runs no consumer). Not runnable in the no-Docker-daemon build
environment used to author this feature -- SKIPS cleanly whenever
Postgres/Redis aren't usable or the required tables don't exist.

Author now; leave unchecked (DEFERRED -- needs a Postgres+Redis host
with the SPEC-08/10 migrations applied).
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


def _seed_job_with_targets(*, workspace_id: uuid.UUID, match_ids: list[uuid.UUID]) -> uuid.UUID:
    """Seed one ``ScrapeJob`` + one PENDING ``ScrapeJobTarget`` row per
    ``match_ids`` (mirrors `test_spider_lock_collision.py::_seed_job_target`,
    generalized to more than one target)."""
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
            total_targets=len(match_ids),
            started_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()
        for match_id in match_ids:
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


def _cleanup_job_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


@dataclass
class _SeededTargets:
    workspace: SeededWorkspace
    scrape_job_id: uuid.UUID
    locked_match_id: uuid.UUID
    deferred_match_id: uuid.UUID


@pytest.fixture()
def seeded_targets() -> Iterator[_SeededTargets]:
    workspace = seed_workspace_with_variant("spec11-observability-codes")
    competitor_id = seed_competitor(workspace, "observability-codes-competitor")
    profile_id = seed_scrape_profile(workspace, "observability-codes-profile")
    unique = uuid.uuid4().hex[:8]
    locked_match_id = seed_match(
        workspace,
        competitor_id,
        f"https://observability-codes-locked-{unique}.invalid/product/1",
        scrape_profile_id=profile_id,
    )
    deferred_match_id = seed_match(
        workspace,
        competitor_id,
        f"https://observability-codes-deferred-{unique}.invalid/product/1",
        scrape_profile_id=profile_id,
    )
    scrape_job_id = _seed_job_with_targets(
        workspace_id=workspace.workspace_id, match_ids=[locked_match_id, deferred_match_id]
    )
    try:
        yield _SeededTargets(
            workspace=workspace,
            scrape_job_id=scrape_job_id,
            locked_match_id=locked_match_id,
            deferred_match_id=deferred_match_id,
        )
    finally:
        _cleanup_job_rows(workspace.workspace_id)
        cleanup_seeded_workspace(workspace)


def test_lock_collision_persists_skipped_locked_already_running(seeded_targets: _SeededTargets) -> None:
    """The ScrapeResult -> `_flush_batch` -> `mark_target` route (T023,
    relying on the T026 broadening) persists SKIPPED + LOCKED_ALREADY_RUNNING."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, ScrapeErrorCode

    from scrape_core.items import ScrapeResult
    from scrape_core.pipelines import _flush_batch

    workspace = seeded_targets.workspace
    match_id = seeded_targets.locked_match_id

    item = ScrapeResult(
        workspace_id=workspace.workspace_id,
        match_id=match_id,
        product_id=workspace.product_id,
        product_variant_id=workspace.product_variant_id,
        competitor_id=next(iter(workspace._competitor_ids)),
        scrape_job_id=seeded_targets.scrape_job_id,
        url=f"https://observability-codes-locked.invalid/product/1",
        access_method=AccessMethod.DIRECT_HTTP,
        attempt_number=1,
        status_code=None,
        success=False,
        error_code=ScrapeErrorCode.LOCKED_ALREADY_RUNNING,
        error_message="match lock already held -- another attempt is in flight",
        scraped_at=datetime.now(UTC),
        # No match_lock_key/match_lock_token -- a collision never
        # acquired a lock, so `_flush_batch` attempts no release.
    )

    _flush_batch(workspace.workspace_id, [item])

    with get_session() as session:
        from sqlalchemy import text

        target_row = session.execute(
            text(
                "SELECT status, error_code FROM scrape_job_targets "
                "WHERE workspace_id = :ws AND scrape_job_id = :job AND match_id = :match_id"
            ),
            {"ws": workspace.workspace_id, "job": seeded_targets.scrape_job_id, "match_id": match_id},
        ).mappings().one()
        assert target_row["status"] == "SKIPPED"
        assert target_row["error_code"] == "LOCKED_ALREADY_RUNNING"


def test_overflow_persists_deferred_rate_limited(seeded_targets: _SeededTargets) -> None:
    """The requeue-cap-overflow route (T027's `_mark_target_deferred_rate_limited`,
    relying on the same T026 broadening) persists DEFERRED + RATE_LIMITED --
    never through `ScrapeResult`, since an overflowed target never dispatches
    a request."""
    from app_shared.database import get_session

    from price_monitor.spiders.generic_price_spider import _mark_target_deferred_rate_limited

    workspace = seeded_targets.workspace
    match_id = seeded_targets.deferred_match_id

    _mark_target_deferred_rate_limited(workspace.workspace_id, seeded_targets.scrape_job_id, match_id)

    with get_session() as session:
        from sqlalchemy import text

        target_row = session.execute(
            text(
                "SELECT status, error_code, started_at, completed_at FROM scrape_job_targets "
                "WHERE workspace_id = :ws AND scrape_job_id = :job AND match_id = :match_id"
            ),
            {"ws": workspace.workspace_id, "job": seeded_targets.scrape_job_id, "match_id": match_id},
        ).mappings().one()
        assert target_row["status"] == "DEFERRED"
        assert target_row["error_code"] == "RATE_LIMITED"
        # Non-terminal: neither timestamp is stamped by a DEFERRED
        # transition (data-model.md §2.1, overflow-dispatch.md §1).
        assert target_row["started_at"] is None
        assert target_row["completed_at"] is None


def test_codes_are_distinguishable(seeded_targets: _SeededTargets) -> None:
    """Both outcomes are attributable and distinct from each other -- the
    SC-006 "100% attributable, distinguishable from other codes" claim."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, ScrapeErrorCode

    from scrape_core.items import ScrapeResult
    from scrape_core.pipelines import _flush_batch

    workspace = seeded_targets.workspace

    item = ScrapeResult(
        workspace_id=workspace.workspace_id,
        match_id=seeded_targets.locked_match_id,
        product_id=workspace.product_id,
        product_variant_id=workspace.product_variant_id,
        competitor_id=next(iter(workspace._competitor_ids)),
        scrape_job_id=seeded_targets.scrape_job_id,
        url="https://observability-codes-locked.invalid/product/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=False,
        error_code=ScrapeErrorCode.LOCKED_ALREADY_RUNNING,
        scraped_at=datetime.now(UTC),
    )
    _flush_batch(workspace.workspace_id, [item])

    from price_monitor.spiders.generic_price_spider import _mark_target_deferred_rate_limited

    _mark_target_deferred_rate_limited(
        workspace.workspace_id, seeded_targets.scrape_job_id, seeded_targets.deferred_match_id
    )

    with get_session() as session:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT match_id, status, error_code FROM scrape_job_targets "
                "WHERE workspace_id = :ws AND scrape_job_id = :job"
            ),
            {"ws": workspace.workspace_id, "job": seeded_targets.scrape_job_id},
        ).mappings().all()
        by_match = {row["match_id"]: (row["status"], row["error_code"]) for row in rows}
        assert by_match[seeded_targets.locked_match_id] == ("SKIPPED", "LOCKED_ALREADY_RUNNING")
        assert by_match[seeded_targets.deferred_match_id] == ("DEFERRED", "RATE_LIMITED")
        assert by_match[seeded_targets.locked_match_id] != by_match[seeded_targets.deferred_match_id]
