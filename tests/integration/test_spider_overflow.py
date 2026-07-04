"""Live SPEC-11 US3 requeue-cap overflow integration test (T024,
`contracts/overflow-dispatch.md`, `contracts/spider-integration.md` §2,
SC-003, US3 AS1/AS2) — ⏸ DEFERRED.

Forces `_acquire_fetch_permission`'s (T013/T027) domain-permission gate
to deny **every** attempt (by monkeypatching `scrape_core.limiter.
acquire_permission` inside the spider's own subprocess to always return
`Permission(granted=False, ...)` -- real Redis token-bucket refill timing
is not what this test is about, and the real per-denial wait is also
monkeypatched (`deferred_delay` -> an already-fired ``Deferred``) so the
test does not spend real wall-clock time on backoff sleeps). With
`REQUEUE_MAX_ATTEMPTS=1` (env override), the spider must:

1. Requeue exactly through the cap (2 denials: the first bumps
   `requeue_count` to 1, not yet `> REQUEUE_MAX_ATTEMPTS`; the second
   bumps it to 2, which overflows) then stop looping.
2. Mark the target `DEFERRED` + `RATE_LIMITED` directly (T026/T027 --
   never through the `ScrapeResult`/pipeline path -- there is no
   observation/attempt row for this match at all).
3. Call `app_shared.messaging.enqueue(SCRAPE_DISPATCH_JOB,
   queue="scrape_dispatch", ...)` **exactly once** (recorded via a
   monkeypatched `enqueue` inside the subprocess -- never a real Celery
   broker round trip, so this test needs no running consumer).
4. Yield **no** `scrapy.Request` for this target -- the target's URL is
   a non-resolvable `.invalid` domain precisely so a real fetch attempt
   would be unmistakable (mirrors `test_spider_lock_collision.py`); no
   `request_attempts`/`price_observations` row is ever created.

Needs a reachable Postgres (`DATABASE_URL`, SPEC-11 migration-free --
only the SPEC-08/10 tables) AND a reachable Redis (`REDIS_URL`, needed
for `app_shared.messaging.enqueue`'s Celery producer to construct even
though the send is never awaited/consumed) with a live Scrapyd-
equivalent crawl execution (this test runs the real spider in its own
OS process, mirroring `test_spider_lock_collision.py`). Not runnable in
the no-Docker-daemon build environment used to author this feature --
SKIPS cleanly whenever Postgres/Redis aren't usable or the required
tables don't exist.

Author now; leave unchecked (DEFERRED -- needs a Postgres+Redis host
with the SPEC-08/10 migrations applied).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
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
    skip and never reaches the SPEC-11 limiter gate at all (mirrors
    `test_spider_lock_collision.py::_create_access_policy`)."""
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
    for `match_id` -- the row `mark_target` needs to resolve in order to
    transition it (mirrors `test_spider_lock_collision.py::_seed_job_target`)."""
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
    workspace = seed_workspace_with_variant("spec11-overflow")
    _create_access_policy(workspace.workspace_id)
    competitor_id = seed_competitor(workspace, "overflow-competitor")
    profile_id = seed_scrape_profile(workspace, "overflow-profile")
    unique = uuid.uuid4().hex[:8]
    match_id = seed_match(
        workspace,
        competitor_id,
        f"https://spider-overflow-{unique}.invalid/product/1",
        scrape_profile_id=profile_id,
    )
    scrape_job_id = _seed_job_target(workspace_id=workspace.workspace_id, match_id=match_id)
    try:
        yield _SeededTarget(workspace=workspace, match_id=match_id, scrape_job_id=scrape_job_id)
    finally:
        _cleanup_access_and_job_rows(workspace.workspace_id)
        cleanup_seeded_workspace(workspace)


# --- custom runner: forces continuous denial + instant backoff + records enqueue --

_OVERFLOW_RUNNER_TEMPLATE = """
import json
import sys

from twisted.internet.defer import Deferred

import price_monitor.spiders.generic_price_spider as spider_mod

# Force EVERY permission acquire to deny -- the real Redis token-bucket
# refill timing is not what this test is about (contracts/spider-integration.md
# step 2); `deferred_delay` is patched to resolve instantly so the test
# never spends real wall-clock time on backoff sleeps.
async def _always_denied_acquire_permission(redis, *, workspace_id, domain, access_method, limits, settings, sem_token):
    return spider_mod.Permission(granted=False, wait_hint_seconds=0.01)


spider_mod.acquire_permission = _always_denied_acquire_permission


def _instant_delay(seconds):
    d = Deferred()
    d.callback(None)
    return d


spider_mod.deferred_delay = _instant_delay

_enqueue_calls = []


def _recording_enqueue(name, *, queue, kwargs=None):
    # Recorded only -- never a real Celery broker round trip, so this
    # test needs no running consumer (contracts/overflow-dispatch.md §3).
    _enqueue_calls.append({{"name": name, "queue": queue, "kwargs": kwargs}})


spider_mod.enqueue = _recording_enqueue

from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

import price_monitor.settings as _base_settings

settings = Settings()
settings.setmodule(_base_settings, priority="project")

process = CrawlerProcess(settings, install_root_handler=False)
process.crawl(
    spider_mod.GenericPriceSpider,
    workspace_id={workspace_id!r},
    scrape_job_id={scrape_job_id!r},
    match_ids={match_ids_arg!r},
    mode="HTTP",
)
process.start()

with open({enqueue_log_path!r}, "w") as f:
    f.write(json.dumps(_enqueue_calls))
"""


def _run_overflow_spider_subprocess(
    *, workspace_id: uuid.UUID, scrape_job_id: uuid.UUID, match_ids: list[uuid.UUID]
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    fd, enqueue_log_path = tempfile.mkstemp(suffix="_overflow_enqueue_calls.json")
    os.close(fd)
    script = _OVERFLOW_RUNNER_TEMPLATE.format(
        workspace_id=str(workspace_id),
        scrape_job_id=str(scrape_job_id),
        match_ids_arg=",".join(str(m) for m in match_ids),
        enqueue_log_path=enqueue_log_path,
    )
    script = textwrap.dedent(script)

    script_fd, script_path = tempfile.mkstemp(suffix="_generic_price_spider_overflow_runner.py")
    try:
        with os.fdopen(script_fd, "w") as fh:
            fh.write(script)

        env = {**os.environ, "REQUEUE_MAX_ATTEMPTS": "1"}
        result = subprocess.run(  # noqa: S603 - fixed interpreter, generated script, test-only
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        enqueue_calls: list[dict[str, object]] = []
        if os.path.exists(enqueue_log_path) and os.path.getsize(enqueue_log_path) > 0:
            with open(enqueue_log_path) as f:
                enqueue_calls = json.load(f)
        return result, enqueue_calls
    finally:
        os.unlink(script_path)
        if os.path.exists(enqueue_log_path):
            os.unlink(enqueue_log_path)


def test_overflow_marks_deferred_enqueues_once_no_fetch(seeded_target: _SeededTarget) -> None:
    from app_shared.database import get_session

    workspace_id = seeded_target.workspace.workspace_id
    match_id = seeded_target.match_id

    result, enqueue_calls = _run_overflow_spider_subprocess(
        workspace_id=workspace_id,
        scrape_job_id=seeded_target.scrape_job_id,
        match_ids=[match_id],
    )
    assert result.returncode == 0, result.stderr

    # No request was ever fetched -- no observation/attempt row exists at
    # all for this match (unlike the SKIPPED/lock-collision path, which
    # does emit a terminal ScrapeResult; overflow marks the target
    # directly and never enters the ScrapeResult/pipeline path).
    with get_session() as session:
        from sqlalchemy import text

        observations = session.execute(
            text("SELECT 1 FROM price_observations WHERE workspace_id = :ws AND match_id = :match_id"),
            {"ws": workspace_id, "match_id": match_id},
        ).all()
        assert observations == []

        attempts = session.execute(
            text("SELECT 1 FROM request_attempts WHERE workspace_id = :ws AND match_id = :match_id"),
            {"ws": workspace_id, "match_id": match_id},
        ).all()
        assert attempts == []

        target_row = session.execute(
            text(
                "SELECT status, error_code FROM scrape_job_targets "
                "WHERE workspace_id = :ws AND scrape_job_id = :job AND match_id = :match_id"
            ),
            {"ws": workspace_id, "job": seeded_target.scrape_job_id, "match_id": match_id},
        ).mappings().one()
        assert target_row["status"] == "DEFERRED"
        assert target_row["error_code"] == "RATE_LIMITED"

    # Re-dispatched via the Celery producer exactly once.
    assert len(enqueue_calls) == 1, enqueue_calls
    call = enqueue_calls[0]
    assert call["name"] == "scrape_dispatch.dispatch_job"
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"]["scrape_job_id"] == str(seeded_target.scrape_job_id)
    assert call["kwargs"]["workspace_id"] == str(workspace_id)
