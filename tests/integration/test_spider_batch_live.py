"""Live reactor-safe batched-persistence spider run (SPEC-07 US5 T048,
FR-016..FR-017, SC-006) — ⏸ DEFERRED.

Runs the real ``generic_price_spider`` (via
``run_generic_price_spider_subprocess``) over **N** fixture matches in one
crawl, all served by the same loopback fixture server at distinct paths
(the same JSON-LD fixture content repeated — one real page fetch per
match, zero real-competitor network calls, FR-021/SC-007), with
``SCRAPE_FLUSH_MAX_ITEMS`` overridden to a small value so the batching
behavior is observable within a crawl this small (the production default
of 50 would only ever trigger the single final flush for a 12-match run,
which wouldn't distinguish "batched" from "one flush at close" — this
test's whole point per SC-006).

Proves, end to end against a real DB (not the mocked ``deferToThread``
seam ``tests/unit/test_persistence_batching.py`` already covers):

1. All **N** matches persist exactly one ``price_observations`` row each
   (``success=true``) — nothing is lost across however many flushes it
   took.
2. The observed Postgres commit count (via a SQLAlchemy ``"commit"``
   engine event writing one line per commit to a file the subprocess
   inherits — ``run_generic_price_spider_subprocess``'s
   ``commit_log_path`` seam) is **≪ N**: with
   ``SCRAPE_FLUSH_MAX_ITEMS=3`` and ``N=12`` this is at most 5 (4 full
   batches + a slack margin for a possible extra time-triggered flush),
   never anywhere near one commit per item.
3. "DB off the reactor thread" (contracts/persistence-pipeline.md) is a
   structural property of ``scrape_core.pipelines.BatchedPersistencePipeline``
   having exactly **one** call site for ``scrape_core.db.run_in_thread``
   (``tests/unit/test_reactor_safe_db.py``/``test_persistence_batching.py``
   already prove that seam offloads correctly with a mocked
   ``deferToThread``) — this live test's distinguishing contribution is
   the batching-count guarantee against a real database, which a mocked
   unit test cannot demonstrate.

Needs a reachable Postgres (``DATABASE_URL``) with the SPEC-07 migration
applied AND a reachable Redis (``REDIS_URL``). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever either isn't usable or the required tables don't exist.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-07 migration applied).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
    run_generic_price_spider_subprocess,
    seed_competitor,
    seed_match,
    seed_scrape_profile,
    seed_workspace_with_variant,
    serve_fixture_pages,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"
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
        "scrape_profiles",
    }
)

pytestmark = pytest.mark.skipif(
    not live_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-07 observations "
        "migration applied -- not available in this environment."
    ),
)

_HOST = "fixture-store.invalid"
_MATCH_COUNT = 12
_FLUSH_MAX_ITEMS = 3
# Large enough that the time-based LoopingCall flush should not fire
# mid-crawl (the crawl should finish well under this), keeping the
# commit count attributable to the size-based flush alone; a slack
# margin still covers one extra flush if it does.
_FLUSH_INTERVAL_SECONDS = 60.0

_RESOLVER_SOURCE = f"""
from twisted.internet import defer

from scrape_core.safety.resolver import SafeResolver


class _TestResolver(SafeResolver):
    def getHostByName(self, name, timeout=()):
        if name == {_HOST!r}:
            return defer.succeed("127.0.0.1")
        return super().getHostByName(name, timeout)
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec07-batch")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def test_n_matches_persist_with_commit_count_much_less_than_n(
    seeded: SeededWorkspace,
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    html = (_FIXTURES_DIR / "jsonld_product.html").read_text(encoding="utf-8")
    pages = {f"/product/{i}": html for i in range(_MATCH_COUNT)}
    server, thread, port = serve_fixture_pages(pages)

    commit_log_fd, commit_log_path = tempfile.mkstemp(suffix="_commit_log.txt")
    os.close(commit_log_fd)

    try:
        competitor_id = seed_competitor(seeded, "batch-fixture-competitor")
        profile_id = seed_scrape_profile(seeded, "batch-fixture-profile")
        match_ids = [
            seed_match(
                seeded,
                competitor_id,
                f"http://{_HOST}:{port}/product/{i}",
                scrape_profile_id=profile_id,
            )
            for i in range(_MATCH_COUNT)
        ]

        result = run_generic_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=uuid.uuid4(),
            match_ids=match_ids,
            resolver_source=_RESOLVER_SOURCE,
            dns_resolver_dotted_path="__main__._TestResolver",
            extra_settings={
                "SCRAPE_FLUSH_MAX_ITEMS": _FLUSH_MAX_ITEMS,
                "SCRAPE_FLUSH_INTERVAL_SECONDS": _FLUSH_INTERVAL_SECONDS,
            },
            commit_log_path=commit_log_path,
        )
        assert result.returncode == 0, result.stderr

        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT match_id, success FROM price_observations "
                    "WHERE workspace_id = :ws AND match_id = ANY(:matches)"
                ),
                {"ws": str(seeded.workspace_id), "matches": [str(m) for m in match_ids]},
            ).fetchall()
            assert len(rows) == _MATCH_COUNT, rows
            assert all(row.success for row in rows)

            current_price_count = session.execute(
                text(
                    "SELECT count(*) FROM match_current_prices "
                    "WHERE workspace_id = :ws AND match_id = ANY(:matches)"
                ),
                {"ws": str(seeded.workspace_id), "matches": [str(m) for m in match_ids]},
            ).scalar_one()
            assert current_price_count == _MATCH_COUNT

        commit_count = Path(commit_log_path).read_text(encoding="utf-8").count("\n")
        # ceil(12 / 3) == 4 full-size flushes expected; allow a little slack
        # for one extra flush (e.g. a stray time-based tick) without ever
        # approaching anywhere near one commit per item (SC-006).
        assert 1 <= commit_count <= 6, commit_count
        assert commit_count < _MATCH_COUNT
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(commit_log_path)
