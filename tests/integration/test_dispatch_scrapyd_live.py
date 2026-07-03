"""Live authenticated, idempotent Scrapyd dispatch (SPEC-07 US4 T047,
FR-018/FR-019, SC-005) — ⏸ DEFERRED.

``tests/unit/test_scrapyd_dispatch.py`` already proves
``app_shared.scrapyd.client.ScrapydDispatchClient``'s auth/idempotency
*logic* exhaustively against a fake ``requests.post`` and a hand-rolled
fake Redis. This live test's distinguishing contribution is proving the
same client against the **real** integration points a fake cannot stand
in for: a real Scrapyd node's HTTP basic-auth enforcement (does a genuine
401 actually happen, not just a stubbed one) and a real Redis ``SET NX``
round trip (does the idempotency guard actually hold under real network
latency/serialization, not an in-memory dict).

Dispatches a real, harmless no-op run: ``workspace_id``/``match_ids`` name
a workspace/match that do not exist, so ``generic_price_spider`` (already
deployed on the Scrapyd node per `apps/scrapers/scrapyd.conf`) finds zero
matches and closes immediately — this test only asserts on the
**dispatch** layer (jobid returned / 401 rejected / idempotent no-op),
never on spider execution results (those are `test_spider_*_live.py`'s
job).

1. Correct credentials -> ``schedule.json`` accepts the run and returns a
   non-empty ``jobid``.
2. Wrong credentials -> the real Scrapyd node's basic-auth layer rejects
   with 401 -> the client raises ``ScrapydAuthError`` and leaves no
   poisoned Redis idempotency key behind.
3. A retried dispatch of the same ``(scrape_job_id, batch_index)`` is a
   no-op: the second ``schedule()`` call returns the **same** jobid as
   the first without scheduling a second run (proven by the Redis-backed
   idempotency key holding a real jobid, not the pending sentinel, after
   the first call — the same invariant
   `tests/unit/test_scrapyd_dispatch.py` proves with a fake Redis, now
   over a real one).

Needs a reachable Redis (``REDIS_URL``) AND a live, authenticated Scrapyd
HTTP node (``SCRAPYD_HTTP_URLS`` reachable, responding to
``daemonstatus.json`` with the configured ``SCRAPYD_USERNAME``/
``SCRAPYD_PASSWORD``, with the ``price_monitor`` project + spider already
deployed). Not runnable in the no-Docker-daemon build environment used to
author this feature — SKIPS cleanly whenever any of those isn't usable.

Author now; leave unchecked (DEFERRED — needs a Scrapyd+Redis host with
`price_monitor`/`generic_price_spider` deployed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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


@dataclass
class _WrongPasswordSettings:
    """Duck-typed ``Settings`` override: the real Scrapyd URL, a deliberately
    wrong password. ``ScrapydDispatchClient`` only reads these four
    attributes off its ``settings`` argument."""

    SCRAPYD_HTTP_URLS: list[str]
    SCRAPYD_USERNAME: str
    SCRAPYD_PASSWORD: str


def _dispatch_args(*, batch_index: int) -> dict[str, object]:
    # A non-existent workspace/match -- `generic_price_spider` will find
    # zero matches and close immediately, so this dispatch is a real but
    # harmless no-op run (this test only asserts on the dispatch layer).
    return {
        "workspace_id": str(uuid.uuid4()),
        "scrape_job_id": str(uuid.uuid4()),
        "match_ids": str(uuid.uuid4()),
        "mode": "HTTP",
        "batch_index": batch_index,
    }


def test_correct_credentials_schedule_and_return_jobid() -> None:
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    client = ScrapydDispatchClient()
    args = _dispatch_args(batch_index=1)
    try:
        jobid = client.schedule(_PROJECT, _SPIDER, **args)
        assert isinstance(jobid, str)
        assert jobid
    finally:
        client._redis.delete(dispatch_key(args["scrape_job_id"], args["batch_index"]))


def test_wrong_credentials_are_rejected_and_leave_no_poisoned_key() -> None:
    from app_shared.config import get_settings
    from app_shared.scrapyd.client import ScrapydAuthError, ScrapydDispatchClient, dispatch_key

    settings = get_settings()
    wrong_settings = _WrongPasswordSettings(
        SCRAPYD_HTTP_URLS=settings.SCRAPYD_HTTP_URLS,
        SCRAPYD_USERNAME=settings.SCRAPYD_USERNAME,
        SCRAPYD_PASSWORD=settings.SCRAPYD_PASSWORD + "-definitely-wrong",
    )
    client = ScrapydDispatchClient(settings=wrong_settings)  # type: ignore[arg-type]
    args = _dispatch_args(batch_index=2)
    key = dispatch_key(args["scrape_job_id"], args["batch_index"])

    with pytest.raises(ScrapydAuthError):
        client.schedule(_PROJECT, _SPIDER, **args)

    assert client._redis.get(key) is None


def test_retried_dispatch_same_batch_index_is_noop_same_jobid() -> None:
    from app_shared.scrapyd.client import ScrapydDispatchClient, dispatch_key

    client = ScrapydDispatchClient()
    args = _dispatch_args(batch_index=3)
    key = dispatch_key(args["scrape_job_id"], args["batch_index"])
    try:
        first_jobid = client.schedule(_PROJECT, _SPIDER, **args)
        second_jobid = client.schedule(_PROJECT, _SPIDER, **args)

        assert first_jobid == second_jobid
        # The idempotency key holds the real jobid (committed), not the
        # transient pending sentinel -- proves the guard round-tripped
        # through a real Redis, not just an in-memory fake.
        assert client._redis.get(key) == first_jobid
    finally:
        client._redis.delete(key)
