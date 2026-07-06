"""Live browser-spider bounded-concurrency + batched-persistence + match-lock
test (SPEC-14 T036, US4 AS1/AS4/AS5, SC-004/007, `contracts/browser-safety.md`
"Concurrency & reactor") — DEFERRED.

Three guarantees, all exercised in one real crawl against several browser-
mode targets on a loopback fixture server:

1. **Bounded concurrency** (SC-004): a concurrency-tracking fixture server
   records the maximum number of simultaneously in-flight requests it ever
   observed across the whole crawl -- must never exceed the configured
   ``BROWSER_CONCURRENT_REQUESTS``/``BROWSER_MAX_CONTEXTS`` (each real
   Chromium context/page is expensive; this project is deliberately low
   bounded, `contracts/browser-safety.md`).
2. **Batched, off-reactor persistence** (SC-007): reuses
   ``run_generic_browser_price_spider_subprocess``'s ``commit_log_path``
   hook (one line appended per DB transaction commit) to prove persisting
   N results does far fewer than N commits -- the unchanged
   ``BatchedPersistencePipeline`` core, off-reactor via ``run_in_thread``.
3. **In-flight match lock** (US4 AS5): one of the seeded matches has its
   match lock pre-held (directly against real Redis, simulating "another
   attempt for this match is already in flight") before the crawl starts
   -- that match's own attempt must record ``LOCKED_ALREADY_RUNNING``
   (never fetched, its ``scrape_job_targets`` row transitions ``SKIPPED``),
   with every other (unlocked) match scraped normally in the same run.

Needs a reachable Postgres (SPEC-08/11 migrations applied) + Redis AND an
installed Chromium binary (``playwright install``) -- this no-container-
engine build environment has neither, so this SKIPS cleanly here (never
faked).

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
an installed Chromium binary).
"""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ._browser_spider_live_support import (
    live_browser_stack_reachable,
    run_generic_browser_price_spider_subprocess,
)
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = frozenset(
    {
        "price_observations",
        "request_attempts",
        "match_current_prices",
        "competitor_product_matches",
        "competitors",
        "product_variants",
        "products",
        "workspaces",
        "scrape_jobs",
        "scrape_job_targets",
        "access_policies",
    }
)

pytestmark = pytest.mark.skipif(
    not live_browser_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-08/11 migrations "
        "applied, AND an installed Playwright Chromium binary -- not available "
        "in this environment."
    ),
)

_SUBPROCESS_TIMEOUT_SECONDS = 90.0
_NUM_TARGETS = 6
# Each fixture response holds the "connection" briefly so overlapping
# in-flight browser sessions are actually observable (rather than the
# whole crawl completing faster than any overlap could ever register).
_RESPONSE_DELAY_SECONDS = 0.5
_FIXTURE_HTML = "<html><body><div id='price'>$4.99</div></body></html>"


class _ConcurrencyTrackingServer(ThreadingHTTPServer):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0


class _Handler(BaseHTTPRequestHandler):
    server: _ConcurrencyTrackingServer

    def do_GET(self) -> None:  # noqa: N802
        server: _ConcurrencyTrackingServer = self.server  # type: ignore[assignment]
        with server.lock:
            server.active += 1
            server.max_active = max(server.max_active, server.active)
        try:
            if not self.path.startswith("/product/"):
                self.send_response(404)
                self.end_headers()
                return
            time.sleep(_RESPONSE_DELAY_SECONDS)
            body = _FIXTURE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with server.lock:
                server.active -= 1

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def _create_access_policy(workspace_id: uuid.UUID) -> uuid.UUID:
    """`WORKSPACE_DEFAULT_POLICY_NAME`-named policy so `_prepare_dispatch`
    reaches the SPEC-11 limiter/lock gate for every seeded match (mirrors
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


def _seed_job_targets(*, workspace_id: uuid.UUID, match_ids: list[uuid.UUID]) -> uuid.UUID:
    from app_shared.enums import ScrapeJobSource, ScrapeJobStatus, ScrapeJobType, ScrapeScope, ScrapeTargetStatus
    from app_shared.database import get_session
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


def _cleanup_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    """The workspace + its `default` access policy only -- the fixture
    server's ephemeral port (needed for the seeded matches' URLs) is only
    known inside the test body, so matches/job targets are seeded there."""
    workspace = seed_workspace_with_variant("spec14-browser-bounds")
    _create_access_policy(workspace.workspace_id)
    try:
        yield workspace
    finally:
        _cleanup_rows(workspace.workspace_id)
        cleanup_seeded_workspace(workspace)


def test_bounded_concurrency_batched_commits_and_locked_match_skip(seeded: SeededWorkspace) -> None:
    from app_shared.config import get_settings
    from app_shared.database import get_session
    from app_shared.limiter.keys import match_lock_key
    from app_shared.limiter.locks import acquire_match_lock, release_match_lock
    from app_shared.redis_client import get_redis_client
    from sqlalchemy import text

    workspace = seeded
    settings = get_settings()

    server = _ConcurrencyTrackingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port

    redis = get_redis_client()
    lock_key = None
    owner_token = f"pre-held-owner-{uuid.uuid4().hex}"

    try:
        competitor_id = seed_competitor(workspace, "browser-bounds-competitor")
        match_ids = [
            seed_match(workspace, competitor_id, f"http://127.0.0.1:{port}/product/{i}")
            for i in range(_NUM_TARGETS)
        ]
        locked_match_id = match_ids[0]
        scrape_job_id = _seed_job_targets(workspace_id=workspace.workspace_id, match_ids=match_ids)

        lock_key = match_lock_key(workspace.workspace_id, locked_match_id)
        assert acquire_match_lock(redis, key=lock_key, token=owner_token, ttl_seconds=600) is True

        with tempfile.NamedTemporaryFile(suffix="_commit_log.txt", delete=False) as fh:
            commit_log_path = fh.name

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=workspace.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=match_ids,
            commit_log_path=commit_log_path,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, result.stderr

        # --- SC-004: bounded concurrency -------------------------------
        assert server.max_active <= settings.BROWSER_CONCURRENT_REQUESTS
        assert server.max_active <= settings.BROWSER_MAX_CONTEXTS

        # --- SC-007: far fewer than N commits, batched off-reactor -----
        with open(commit_log_path) as fh:
            commit_count = sum(1 for _ in fh)
        assert commit_count < _NUM_TARGETS
        assert commit_count >= 1

        # --- US4 AS5: the locked match was skipped, never fetched ------
        with get_session() as session:
            locked_attempt = session.execute(
                text(
                    "SELECT success, error_code, status_code FROM request_attempts "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": workspace.workspace_id, "match": locked_match_id},
            ).mappings().all()
            assert len(locked_attempt) == 1, locked_attempt
            assert locked_attempt[0]["success"] is False
            assert locked_attempt[0]["error_code"] == "LOCKED_ALREADY_RUNNING"
            assert locked_attempt[0]["status_code"] is None  # never fetched

            locked_target = session.execute(
                text(
                    "SELECT status FROM scrape_job_targets "
                    "WHERE workspace_id = :ws AND scrape_job_id = :job AND match_id = :match"
                ),
                {"ws": workspace.workspace_id, "job": scrape_job_id, "match": locked_match_id},
            ).mappings().one()
            assert locked_target["status"] == "SKIPPED"

            # Every OTHER (unlocked) match was scraped normally in the
            # same run -- no duplicate/blocked scrape beyond the one
            # deliberately pre-held match.
            for match_id in match_ids:
                if match_id == locked_match_id:
                    continue
                attempts = session.execute(
                    text(
                        "SELECT success FROM request_attempts "
                        "WHERE workspace_id = :ws AND match_id = :match"
                    ),
                    {"ws": workspace.workspace_id, "match": match_id},
                ).fetchall()
                assert len(attempts) == 1, (match_id, attempts)
    finally:
        if lock_key is not None:
            release_match_lock(redis, key=lock_key, token=owner_token)
        server.shutdown()
        server.server_close()
