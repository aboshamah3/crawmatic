"""Live real-Redis per-variant-per-job recompute dedup (SPEC-09 US3/T038,
FR-012/FR-015, SC-007) — ⏸ DEFERRED.

`tests/unit/test_recompute_triggers_pipeline.py` already proves the
dedup logic in `scrape_core.pipelines._flush_batch` (trigger a) against
a fake Redis honoring `SET NX`. This live test's distinguishing
contribution: a **real** Redis `SET NX` actually serializing the race
(`analysis:enqueued:{scrape_job_id}:{product_variant_id}`) across many
completed matches of one variant within one job, driven through the
real `_flush_batch` (real Postgres transaction + real
`app_shared.messaging.enqueue` Celery producer over the real Redis
broker), asserted via the `price_analysis` Redis list length — exactly
the technique `tests/integration/test_jobs_run_variant_live.py`
(SPEC-08) uses for the `scrape_dispatch` queue.

Per contracts/recompute-triggers.md trigger (a):

1. A batch of N `ScrapeResult` items, all naming the same
   `(workspace_id, scrape_job_id, product_variant_id)`, flushed through
   `_flush_batch` -> exactly **one** `PRICE_ANALYSIS_RECOMPUTE` lands on
   the real `price_analysis` Redis list for that variant/job (many
   completions collapse to one recompute).
2. A second batch for the SAME variant but a **different**
   `scrape_job_id` (a fresh job) -> the dedup key differs -> one MORE
   enqueue (a fresh job always re-enqueues, never suppressed by a prior
   job's key).
3. An ad-hoc item (`scrape_job_id=None`) enqueues directly, no dedup key
   involved, one enqueue per such item.

Needs a reachable Postgres (`DATABASE_URL`, SPEC-09 migration applied —
`_flush_batch` writes `price_observations`/`request_attempts`/
`match_current_prices`) AND a reachable Redis (`REDIS_URL`, the dedup
key + the Celery producer's broker). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever either isn't reachable.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ._alerts_live_support import alerts_live_reachable
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

pytestmark = pytest.mark.skipif(
    not alerts_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, SPEC-09 migration applied) "
        "and a reachable Redis (REDIS_URL) in this environment."
    ),
)


@dataclass
class _Fixture:
    seeded: SeededWorkspace
    competitor_id: uuid.UUID


@pytest.fixture()
def fixture() -> Iterator[_Fixture]:
    seeded = seed_workspace_with_variant("recompute-dedup-live")
    competitor_id = seed_competitor(seeded, "Recompute Dedup Live Competitor")
    try:
        yield _Fixture(seeded=seeded, competitor_id=competitor_id)
    finally:
        from sqlalchemy import text

        from app_shared.database import get_session

        with get_session() as session:
            session.execute(
                text("DELETE FROM variant_price_states WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.execute(
                text("DELETE FROM variant_alert_states WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.commit()
        cleanup_seeded_workspace(seeded)


@pytest.fixture()
def redis_client():
    from app_shared.redis_client import get_redis_client

    return get_redis_client()


def _make_item(fixture: _Fixture, *, match_id, scrape_job_id):
    from app_shared.enums import AccessMethod
    from scrape_core.items import ScrapeResult

    return ScrapeResult(
        workspace_id=fixture.seeded.workspace_id,
        match_id=match_id,
        product_id=fixture.seeded.product_id,
        product_variant_id=fixture.seeded.product_variant_id,
        competitor_id=fixture.competitor_id,
        scrape_job_id=scrape_job_id,
        url="https://recompute-dedup-live.invalid/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        scraped_at=datetime.now(UTC),
        price=Decimal("42.0000"),
        currency="USD",
        success=True,
        comparable=True,
    )


def test_many_completions_of_one_variant_in_one_job_collapse_to_one_recompute(
    fixture: _Fixture, redis_client
) -> None:
    from scrape_core.pipelines import _flush_batch

    scrape_job_id = uuid.uuid4()
    match_ids = [
        seed_match(fixture.seeded, fixture.competitor_id, f"https://recompute-dedup-live.invalid/a/{i}")
        for i in range(4)
    ]
    batch = [_make_item(fixture, match_id=m, scrape_job_id=scrape_job_id) for m in match_ids]

    before = redis_client.llen("price_analysis")
    _flush_batch(fixture.seeded.workspace_id, batch)
    after = redis_client.llen("price_analysis")

    assert after - before == 1, (
        f"expected exactly one PRICE_ANALYSIS_RECOMPUTE enqueued, got delta={after - before}"
    )


def test_a_fresh_job_for_the_same_variant_re_enqueues(fixture: _Fixture, redis_client) -> None:
    from scrape_core.pipelines import _flush_batch

    match_id_job1 = seed_match(
        fixture.seeded, fixture.competitor_id, "https://recompute-dedup-live.invalid/b/1"
    )
    job1 = uuid.uuid4()
    _flush_batch(fixture.seeded.workspace_id, [_make_item(fixture, match_id=match_id_job1, scrape_job_id=job1)])

    before = redis_client.llen("price_analysis")

    match_id_job2 = seed_match(
        fixture.seeded, fixture.competitor_id, "https://recompute-dedup-live.invalid/b/2"
    )
    job2 = uuid.uuid4()
    _flush_batch(fixture.seeded.workspace_id, [_make_item(fixture, match_id=match_id_job2, scrape_job_id=job2)])

    after = redis_client.llen("price_analysis")
    assert after - before == 1, (
        f"a fresh job should re-enqueue exactly once, got delta={after - before}"
    )


def test_ad_hoc_item_with_no_scrape_job_id_enqueues_directly(
    fixture: _Fixture, redis_client
) -> None:
    from scrape_core.pipelines import _flush_batch

    match_id = seed_match(
        fixture.seeded, fixture.competitor_id, "https://recompute-dedup-live.invalid/c/1"
    )
    before = redis_client.llen("price_analysis")
    _flush_batch(
        fixture.seeded.workspace_id,
        [_make_item(fixture, match_id=match_id, scrape_job_id=None)],
    )
    after = redis_client.llen("price_analysis")
    assert after - before == 1
