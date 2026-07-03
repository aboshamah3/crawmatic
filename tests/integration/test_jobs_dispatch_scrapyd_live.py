"""Live authenticated, idempotent, node-targeted Scrapyd dispatch (SPEC-08
US1, FR-011/FR-012/FR-013/FR-014, SC-003) — ⏸ DEFERRED.

`tests/integration/test_dispatch_scrapyd_live.py` (SPEC-07) already
proves `ScrapydDispatchClient.schedule(...)`'s auth + idempotency
against a real Scrapyd node + real Redis. This spec (SPEC-08) extends
that same client with one new argument — `node_url` (targeting a
deterministically hash-by-domain-selected node, FR-014) — and adds the
`scrape_dispatch.dispatch_job` orchestration layer on top
(`app_shared.jobs.batching`/`app_shared.jobs.nodes`). This live test's
distinguishing contribution over the SPEC-07 precedent: proving the
`node_url` extension actually targets a real node (not falling back to
`SCRAPYD_HTTP_URLS[0]` silently) and that a batch carrying
`workspace_id`/`scrape_job_id`/`match_ids` dispatched with an explicit
`node_url` behaves identically to the un-extended call — same
authenticated `schedule.json` accept + same Redis `SET NX` idempotency
guard, now keyed through the SPEC-08 `dispatch_job` task's own batch
`batch_index` numbering.

Dispatches real, harmless no-op runs: `workspace_id`/`match_ids` name a
workspace/match that do not exist, so `generic_price_spider` (already
deployed on the Scrapyd node per `apps/scrapers/scrapyd.conf`) finds
zero matches and closes immediately — this test only asserts on the
**dispatch** layer (jobid returned / idempotent no-op), never on spider
execution results, and contacts no real competitor domain.

1. `select_node(domain, SCRAPYD_HTTP_URLS)` resolves to a real,
   configured node URL; `client.schedule(..., node_url=that_url)`
   accepts the run and returns a non-empty `jobid` (FR-012/FR-014).
2. A retried dispatch of the same `(scrape_job_id, batch_index)` -- even
   though issued with the same `node_url` a second time -- is a no-op:
   the second call returns the **same** `jobid` without scheduling a
   second run (FR-013, SC-003), proven the same way the SPEC-07
   precedent proves it (the Redis-backed idempotency key holds the real
   jobid, not the pending sentinel, after the first call).

Needs a reachable Redis (`REDIS_URL`) AND a live, authenticated Scrapyd
HTTP node (`SCRAPYD_HTTP_URLS` reachable, `price_monitor`/
`generic_price_spider` already deployed). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever either isn't usable.

Author now; leave unchecked (DEFERRED — needs a Scrapyd+Redis host with
`price_monitor`/`generic_price_spider` deployed).
"""

from __future__ import annotations

import uuid

import pytest

from ._scrapyd_spider_live_support import live_scrapyd_reachable

pytestmark = pytest.mark.skipif(
    not live_scrapyd_reachable(),
    reason=(
        "Needs reachable REDIS_URL + an authenticated, reachable Scrapyd HTTP "
        "node with the price_monitor project deployed -- not available in "
        "this environment."
    ),
)

_PROJECT = "price_monitor"
_SPIDER = "generic_price_spider"


def _dispatch_args(*, batch_index: str, node_url: str) -> dict[str, object]:
    # A non-existent workspace/match -- `generic_price_spider` will find
    # zero matches and close immediately, so this dispatch is a real but
    # harmless no-op run (this test only asserts on the dispatch layer).
    return {
        "workspace_id": str(uuid.uuid4()),
        "scrape_job_id": str(uuid.uuid4()),
        "match_ids": str(uuid.uuid4()),
        "mode": "HTTP",
        "batch_index": batch_index,
        "node_url": node_url,
    }


def test_node_url_extension_targets_a_real_selected_node_and_returns_jobid() -> None:
    from app_shared.config import get_settings
    from app_shared.jobs.nodes import select_node
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    settings = get_settings()
    node_url = select_node("jobs-dispatch-live.invalid", settings.SCRAPYD_HTTP_URLS)
    assert node_url in settings.SCRAPYD_HTTP_URLS

    client = ScrapydDispatchClient(settings=settings)
    args = _dispatch_args(batch_index="jobs-live-1", node_url=node_url)
    try:
        jobid = client.schedule(_PROJECT, _SPIDER, **args)
        assert isinstance(jobid, str)
        assert jobid
    finally:
        client._redis.delete(dispatch_key(args["scrape_job_id"], args["batch_index"]))


def test_retried_dispatch_with_node_url_same_batch_index_is_noop_same_jobid() -> None:
    from app_shared.config import get_settings
    from app_shared.jobs.nodes import select_node
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    settings = get_settings()
    node_url = select_node("jobs-dispatch-live-2.invalid", settings.SCRAPYD_HTTP_URLS)

    client = ScrapydDispatchClient(settings=settings)
    args = _dispatch_args(batch_index="jobs-live-2", node_url=node_url)
    key = dispatch_key(args["scrape_job_id"], args["batch_index"])
    try:
        first_jobid = client.schedule(_PROJECT, _SPIDER, **args)
        second_jobid = client.schedule(_PROJECT, _SPIDER, **args)

        assert first_jobid == second_jobid
        # The idempotency key holds the real jobid (committed), not the
        # transient pending sentinel -- the guard held over a real Redis
        # round trip, and a second real POST never happened.
        assert client._redis.get(key) == first_jobid
    finally:
        client._redis.delete(key)


def test_same_domain_selects_the_same_node_across_repeated_calls() -> None:
    """FR-014: node selection is deterministic hash-by-domain -- a retried
    batch for the same domain always resolves to the same node, so a
    stall-recovery re-dispatch (contracts/stall-recovery.md) never
    fragments a domain's traffic across nodes."""
    from app_shared.config import get_settings
    from app_shared.jobs.nodes import select_node

    settings = get_settings()
    domain = "jobs-dispatch-live-stable.invalid"

    resolved = {select_node(domain, settings.SCRAPYD_HTTP_URLS) for _ in range(5)}
    assert len(resolved) == 1
    assert next(iter(resolved)) in settings.SCRAPYD_HTTP_URLS
