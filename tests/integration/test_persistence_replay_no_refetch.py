"""Durable spool replay against a real Postgres (F05, plan task B1) — ⏸ DEFERRED.

What this proves that no unit test can
--------------------------------------
``tests/unit/test_pipeline_flush_retry.py`` proves the *pipeline* defers
and replays a failed batch. It cannot prove the thing that makes the
replay safe, because that lives in the database: the two unique keys
revision ``a4e91c7d2b58`` adds
(``uq_price_observations_workspace_id_attempt_uuid_scraped_at`` and
``uq_request_attempts_workspace_id_attempt_uuid_created_at``) and the
``ON CONFLICT DO NOTHING`` inserts that arbitrate on them. Against a real
Postgres this test asserts the whole loop:

1. two results are spooled and a flush is attempted while Postgres is
   **paused** (``docker pause`` — a genuine mid-flush outage, not a
   mocked exception): the flush fails and the spool still holds both rows;
2. Postgres is unpaused and :meth:`replay_pending` runs: the batch is
   persisted, and the spool drains;
3. a SECOND replay of the same rows (the crash window this whole feature
   exists for — a process killed after COMMIT but before the spool rows
   were deleted) writes **nothing new**: exactly one ``request_attempts``
   row per ``attempt_uuid`` and exactly one ``price_observations`` row;
4. the fetcher was called exactly twice in the whole test — a replay
   re-persists, it never re-scrapes. That is the difference between this
   spool and "just crawl it again", and it is the property that keeps a
   retry from costing proxy money a second time.

Needs a reachable Postgres (``DATABASE_URL``) with this plan's migration
applied, a reachable Redis (``REDIS_URL``), and a docker daemon holding
the compose ``postgres`` service. SKIPS cleanly whenever any of those is
missing — which includes the authoring host, whose compose file
``expose:``s Postgres on the internal network only and publishes no host
port, so nothing on the host can reach it.

Author now; leave unchecked (DEFERRED — needs a host that can actually
reach the compose Postgres).
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from twisted.internet.defer import Deferred, fail as defer_fail, succeed
from twisted.internet.task import Clock
from twisted.python.failure import Failure

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
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
    }
)

_COMPOSE_POSTGRES_SERVICE = "postgres"


def _compose_postgres_container() -> str | None:
    """The compose ``postgres`` container id, or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "-q", _COMPOSE_POSTGRES_SERVICE],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - no docker daemon / no compose
        return None
    container = result.stdout.strip().splitlines()
    return container[0] if result.returncode == 0 and container else None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not live_stack_reachable(_REQUIRED_TABLES),
        reason="No reachable Postgres/Redis with the observation tables in this environment",
    ),
    pytest.mark.skipif(
        _compose_postgres_container() is None,
        reason="No compose `postgres` container to pause/unpause",
    ),
]


class _CountingFetcher:
    """Stands in for the downloader: counts every fetch it is asked for.

    The whole point of the spool is that a persistence retry never
    reaches this object again.
    """

    def __init__(self, seeded: SeededWorkspace, competitor_id: uuid.UUID) -> None:
        self.calls = 0
        self._seeded = seeded
        self._competitor_id = competitor_id

    def fetch(self, match_id: uuid.UUID) -> Any:
        from app_shared.enums import AccessMethod, ExtractionMethod, StockStatus

        from scrape_core.items import ScrapeResult

        self.calls += 1
        return ScrapeResult(
            workspace_id=self._seeded.workspace_id,
            match_id=match_id,
            product_id=self._seeded.product_id,
            product_variant_id=self._seeded.product_variant_id,
            competitor_id=self._competitor_id,
            scrape_job_id=None,
            url=f"https://replay-{match_id}.invalid/p",
            access_method=AccessMethod.DIRECT_HTTP,
            success=True,
            price=Decimal("19.99"),
            currency="SAR",
            stock_status=StockStatus.IN_STOCK,
            extraction_method=ExtractionMethod.JSON_LD,
            extraction_confidence=Decimal("0.9500"),
        )


class _SyncRunInThread:
    """``run_in_thread`` without a reactor: run inline, fire immediately.

    This test is about what reaches Postgres, not about the reactor hop
    (``tests/unit/test_persistence_batching.py`` owns that seam), and no
    reactor loop is running in a pytest process.
    """

    def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        try:
            return succeed(fn(*args, **kwargs))
        except Exception:  # noqa: BLE001 - mirrors deferToThread's error path
            return defer_fail(Failure())


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("replay")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def _row_counts(workspace_id: uuid.UUID, attempt_uuids: list[uuid.UUID]) -> tuple[int, int]:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        attempts = session.execute(
            text(
                "SELECT count(*) FROM request_attempts "
                "WHERE workspace_id = :ws AND attempt_uuid = ANY(:ids)"
            ),
            {"ws": workspace_id, "ids": attempt_uuids},
        ).scalar_one()
        observations = session.execute(
            text(
                "SELECT count(*) FROM price_observations "
                "WHERE workspace_id = :ws AND attempt_uuid = ANY(:ids)"
            ),
            {"ws": workspace_id, "ids": attempt_uuids},
        ).scalar_one()
    return int(attempts), int(observations)


def test_replay_persists_exactly_once_and_never_refetches(
    seeded: SeededWorkspace, tmp_path: Any, monkeypatch: Any
) -> None:
    from scrape_core import pipelines as pipelines_mod
    from scrape_core.pipelines import BatchedPersistencePipeline

    container = _compose_postgres_container()
    assert container is not None  # guarded by pytestmark

    competitor_id = seed_competitor(seeded, "replay-competitor")
    match_ids = [
        seed_match(seeded, competitor_id, f"https://replay-{index}.invalid/p")
        for index in range(2)
    ]
    fetcher = _CountingFetcher(seeded, competitor_id)
    results = [fetcher.fetch(match_id) for match_id in match_ids]
    assert fetcher.calls == 2

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _SyncRunInThread())
    pipeline = BatchedPersistencePipeline(
        max_items=2,
        interval_seconds=3600.0,
        spool_path=tmp_path / "replay-spool.sqlite",
        max_pending_batches=8,
        retry_backoff_seconds=(3600.0,),  # never fires inside this test
        quarantine_after=99,
        clock=Clock(),
    )

    paused = False
    try:
        # --- 1. a real mid-flush outage ---------------------------------
        # A fresh connection must be attempted while Postgres is frozen,
        # so the pool's existing sockets are dropped first and libpq is
        # given a short connect deadline (a paused container drops
        # packets rather than refusing them, so an unbounded connect
        # would hang instead of failing).
        from app_shared.database import get_engine

        monkeypatch.setenv("PGCONNECT_TIMEOUT", "5")
        get_engine().dispose()
        subprocess.run(["docker", "pause", container], check=True, timeout=60)
        paused = True

        pipeline.process_item(results[0], spider=None)
        pipeline.process_item(results[1], spider=None)  # size threshold -> flush fails

        attempt_uuids = [result.attempt_id for result in results]
        assert all(attempt_uuid is not None for attempt_uuid in attempt_uuids)
        # Nothing persisted, nothing lost: the batch is still on disk.
        assert pipeline.spool.pending_count() == 2

        # --- 2. recover and replay --------------------------------------
        subprocess.run(["docker", "unpause", container], check=True, timeout=60)
        paused = False
        get_engine().dispose()

        pipeline.replay_pending()

        assert pipeline.spool.pending_count() == 0
        assert _row_counts(seeded.workspace_id, attempt_uuids) == (2, 2)

        # --- 3. the crash window: replay a batch that already committed --
        pipeline.spool.append_batch(results)
        assert pipeline.spool.pending_count() == 2
        pipeline.replay_pending()

        assert pipeline.spool.pending_count() == 0
        # Still exactly one row per attempt identity -- `ON CONFLICT DO
        # NOTHING` on the F05 keys, not a second price point.
        assert _row_counts(seeded.workspace_id, attempt_uuids) == (2, 2)

        # --- 4. a replay re-persists, it never re-scrapes ---------------
        assert fetcher.calls == 2
    finally:
        if paused:
            # Always, on every path: a paused Postgres left behind would
            # break every other test on this host.
            subprocess.run(["docker", "unpause", container], check=False, timeout=60)
        pipeline.spool.close()
