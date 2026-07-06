"""Live authenticated, idempotent browser-node Scrapyd dispatch (SPEC-14
T023, US2, FR-013/015/016, `contracts/dispatch-routing.md`) — ⏸ DEFERRED.

`tests/integration/test_dispatch_scrapyd_live.py` (SPEC-07) and
`tests/integration/test_jobs_dispatch_scrapyd_live.py` (SPEC-08) already
prove `ScrapydDispatchClient.schedule(...)`'s basic-auth + Redis `SET NX`
idempotency against a real, authenticated Scrapyd **HTTP** node. This
test's distinguishing contribution: proving the exact same client
behaves identically against the real, authenticated Scrapyd **browser**
node (`SCRAPYD_BROWSER_URLS`, SPEC-14 US2's dispatch-routing fix) —
same basic-auth acceptance, same idempotency guard, no double-run on a
retried delivery of the same `(scrape_job_id, batch_index)`.

Dispatches a real, harmless no-op run: `workspace_id`/`match_ids` name a
workspace/match that do not exist, so `generic_browser_price_spider`
(already deployed on the browser Scrapyd node per
`apps/scrapers-browser/scrapyd.conf`, SPEC-01) finds zero matches and
closes immediately — this test only asserts on the **dispatch** layer
(jobid returned / idempotent no-op / auth accepted), never on spider
execution results, and contacts no real competitor domain.

1. Correct credentials against the browser node's `schedule.json` ->
   accepted, non-empty `jobid` returned — same basic-auth path as the
   HTTP node (FR-013).
2. A retried dispatch of the same `(scrape_job_id, batch_index)` against
   the browser node is a no-op: the second call returns the **same**
   `jobid` without scheduling a second run (FR-016, SC-008) — the
   Redis-backed idempotency key holds the real jobid, not the pending
   sentinel, after the first call, exactly as the HTTP-node precedent
   proves.

Needs a reachable Redis (`REDIS_URL`) AND a live, authenticated Scrapyd
**browser** node (`SCRAPYD_BROWSER_URLS` reachable, responding to
`daemonstatus.json` with the configured `SCRAPYD_USERNAME`/
`SCRAPYD_PASSWORD`, with the `price_monitor_browser` project +
`generic_browser_price_spider` already deployed, SPEC-01). Not runnable
in the no-Docker-daemon build environment used to author this feature —
SKIPS cleanly whenever either isn't usable.

Author now; leave unchecked (DEFERRED — needs a browser Scrapyd+Redis
host with `price_monitor_browser`/`generic_browser_price_spider`
deployed).
"""

from __future__ import annotations

import uuid

import pytest

from ._scrapyd_spider_live_support import (
    live_scrapyd_browser_reachable,
    live_scrapyd_reachable,
)

pytestmark = pytest.mark.skipif(
    not (live_scrapyd_reachable() and live_scrapyd_browser_reachable()),
    reason=(
        "Needs reachable REDIS_URL + an authenticated, reachable Scrapyd "
        "BROWSER node (SCRAPYD_BROWSER_URLS) with the price_monitor_browser "
        "project deployed -- not available in this environment."
    ),
)

_PROJECT = "price_monitor_browser"
_SPIDER = "generic_browser_price_spider"


def _dispatch_args(*, batch_index: str) -> dict[str, object]:
    # A non-existent workspace/match -- `generic_browser_price_spider`
    # will find zero matches and close immediately, so this dispatch is
    # a real but harmless no-op run (this test only asserts on the
    # dispatch layer).
    return {
        "workspace_id": str(uuid.uuid4()),
        "scrape_job_id": str(uuid.uuid4()),
        "match_ids": str(uuid.uuid4()),
        "mode": "BROWSER",
        "batch_index": batch_index,
    }


def test_browser_node_accepts_basic_auth_same_as_http_node() -> None:
    """FR-013: the browser-node `schedule.json` call authenticates with
    the same `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD` basic-auth the HTTP
    node already uses -- proven by a real accept (non-empty jobid), not
    a 401, against the live browser node."""
    from app_shared.config import get_settings
    from app_shared.jobs.nodes import select_node
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    settings = get_settings()
    node_url = select_node("dispatch-browser-live.invalid", settings.SCRAPYD_BROWSER_URLS)
    assert node_url in settings.SCRAPYD_BROWSER_URLS

    client = ScrapydDispatchClient(settings=settings)
    args = _dispatch_args(batch_index="browser-live-1")
    key = dispatch_key(args["scrape_job_id"], args["batch_index"])
    try:
        jobid = client.schedule(_PROJECT, _SPIDER, node_url=node_url, **args)
        assert isinstance(jobid, str)
        assert jobid
    finally:
        client._redis.delete(key)


def test_retried_browser_dispatch_same_batch_index_is_noop_same_jobid() -> None:
    """FR-016/SC-008: a retried dispatch of the same browser batch never
    double-runs -- the second `schedule()` call returns the same jobid
    as the first, and the idempotency key holds the real (committed)
    jobid, not the transient pending sentinel."""
    from app_shared.config import get_settings
    from app_shared.jobs.nodes import select_node
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    settings = get_settings()
    node_url = select_node("dispatch-browser-live-2.invalid", settings.SCRAPYD_BROWSER_URLS)

    client = ScrapydDispatchClient(settings=settings)
    args = _dispatch_args(batch_index="browser-live-2")
    key = dispatch_key(args["scrape_job_id"], args["batch_index"])
    try:
        first_jobid = client.schedule(_PROJECT, _SPIDER, node_url=node_url, **args)
        second_jobid = client.schedule(_PROJECT, _SPIDER, node_url=node_url, **args)

        assert first_jobid == second_jobid
        assert client._redis.get(key) == first_jobid
    finally:
        client._redis.delete(key)
