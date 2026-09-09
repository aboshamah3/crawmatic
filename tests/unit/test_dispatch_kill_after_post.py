"""The crash-after-POST window, closed end to end (EPA B2 / F06).

Two claims, and they are the two the audit actually made:

1. **A worker that dies after Scrapyd accepted the POST must not cause a
   second POST.** The intent row is committed `POSTED` *before* the
   network call, carrying the node it went to and the `scrapyd_job_id` we
   chose, so the ambiguity survives the process that created it. The
   maintenance reconciler then asks that node, finds the run, and moves
   the row to `CONFIRMED`. `post_count` stays at 1, and the batch is now
   stampable -- `get_committed_dispatch` answers, which is the exact
   proof `stamp_targets_dispatched` demands before it will write
   `dispatched_at`.

2. **A node that has no record of the job is re-POSTed with the SAME
   id.** Absence moves the row to `RECONCILED_MISSING` -- the only state
   from which a re-POST is authorized -- and the re-POST carries the
   identical `jobid`, so a node that *did* receive the original request
   (finished history is capped at 100 entries and lost on restart, so
   "absent" is bounded evidence) dedups it rather than running the batch
   twice.

Everything below drives the REAL `ScrapydDispatchClient`, the REAL
`DispatchIntentStore` in its short-transaction mode, and the REAL
`reconcile_inflight_intents`, over a fake Scrapyd node, a fake Redis and
`FakeOrmSession`. Nothing here can reach a real node: the fake HTTP
transport is the only `post`/`get` in play.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.enums import DispatchIntentState, ScrapeJobStatus
from app_shared.jobs.dispatch_intents import (
    DispatchIntentStore,
    reconcile_inflight_intents,
)
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.scrapyd.client import ScrapydDispatchClient
from app_shared.scrapyd.identity import build_dispatch_identity, get_committed_dispatch

from unit._jobs_fake_session import FakeOrmSession

_NODE = "http://scrapers-a:6800"
_PROJECT = "price_monitor"
_SPIDER = "generic_price_spider"


class ProcessDied(BaseException):
    """The worker being killed — deliberately NOT an `Exception`.

    A `SystemExit`-shaped kill is what actually happens (SIGTERM, an OOM
    reaper, a container stop), and the point of the test is that the
    dispatch path's own `except Exception` cleanup does **not** run: the
    row must be left `POSTED`, not tidied into `FAILED`, because nobody
    at that moment knows which of the two it is.
    """


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, *names):
        return sum(1 for name in names if self.store.pop(name, None) is not None)


class FakeScrapydNode:
    """One node: it accepts POSTs, remembers job ids, and can forget them.

    `list_jobs` has `ScrapydDispatchClient.list_jobs`'s contract — a set
    of ids, and *raising* rather than answering an empty set when the
    node is unreachable, because "no answer" must never be read as "no
    such job".
    """

    def __init__(self) -> None:
        self.posted_jobids: list[str] = []
        self.known: set[str] = set()
        self.reachable = True

    @property
    def post_count(self) -> int:
        return len(self.posted_jobids)

    def forget_all(self) -> None:
        """The node lost its memory — a restart, or a run that aged out."""
        self.known.clear()

    # --- the two seams the code under test uses -------------------------
    def post(self, url, *, data, auth, timeout):
        jobid = str(data.get("jobid") or uuid.uuid1().hex)
        self.posted_jobids.append(jobid)
        self.known.add(jobid)
        return SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok", "jobid": jobid}
        )

    def list_jobs(self, node_url, project=None):
        if not self.reachable:
            raise requests.ConnectionError(f"{node_url} unreachable")
        return set(self.known)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        SCRAPYD_HTTP_URLS=[_NODE],
        SCRAPYD_BROWSER_URLS=["http://scrapers-browser:6800"],
        SCRAPYD_USERNAME="scrapyd",
        SCRAPYD_PASSWORD="change-me",
        SCRAPYD_DISPATCH_GUARD_TTL_SECONDS=900,
        SCRAPYD_DETERMINISTIC_JOBID=True,
    )


class _World:
    def __init__(self) -> None:
        self.workspace_id = uuid.uuid4()
        self.job_id = uuid.uuid4()
        self.session = FakeOrmSession()
        job = ScrapeJob(
            workspace_id=self.workspace_id,
            status=ScrapeJobStatus.RUNNING,
            total_targets=1,
            cancellation_generation=0,
            planning_generation=1,
        )
        job.id = self.job_id
        self.session.seed(job)
        self.node = FakeScrapydNode()
        self.redis = FakeRedis()
        self.identity = build_dispatch_identity(
            scrape_job_id=str(self.job_id),
            planning_generation=1,
            strategy_method="HTTP/JSONLD/v1",
            domain="shop.example.com",
            mode="HTTP",
            node_class=f"{_PROJECT}:{_SPIDER}",
            match_ids=["m1", "m2"],
        )

    @contextmanager
    def factory(self) -> Iterator[FakeOrmSession]:
        yield self.session

    def store(self) -> DispatchIntentStore:
        return DispatchIntentStore(
            None,
            workspace_id=self.workspace_id,
            scrape_job_id=self.job_id,
            session_factory=self.factory,
        )

    def client(self, intents: DispatchIntentStore) -> ScrapydDispatchClient:
        http = requests.Session()
        http.post = self.node.post
        return ScrapydDispatchClient(
            settings=_settings(),
            redis_client=self.redis,
            session=http,
            intents=intents,
        )

    def intent(self) -> DispatchIntent:
        rows = self.session._rows[DispatchIntent]  # noqa: SLF001 - the fake's storage
        assert len(rows) == 1
        return rows[0]

    def schedule(self, client: ScrapydDispatchClient, jobid: str) -> str:
        return client.schedule(
            _PROJECT,
            _SPIDER,
            workspace_id=str(self.workspace_id),
            scrape_job_id=str(self.job_id),
            match_ids=["m1", "m2"],
            mode="HTTP",
            batch_index=0,
            node_url=_NODE,
            identity=self.identity,
            jobid=jobid,
        )


@pytest.fixture()
def world() -> _World:
    return _World()


def test_worker_death_after_accepted_post_does_not_repost(world: _World) -> None:
    store = world.store()
    planned = store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    jobid = str(planned.scrapyd_job_id)

    # The worker dies between Scrapyd's `status=ok` and the CONFIRMED
    # write. `confirm` is where that boundary lives, so that is where the
    # kill is injected.
    def _die(identity, scrapyd_job_id):
        raise ProcessDied("SIGKILL between the POST and the confirm")

    store.confirm = _die  # type: ignore[method-assign]
    with pytest.raises(ProcessDied):
        world.schedule(world.client(store), jobid)

    row = world.intent()
    assert row.state == DispatchIntentState.POSTED
    assert row.node_url == _NODE
    assert str(row.scrapyd_job_id) == jobid
    assert world.node.post_count == 1

    # The reconciler asks the node the row named. The run is there.
    report = reconcile_inflight_intents(world.factory, world.node)

    assert report.examined == 1 and report.confirmed == 1 and report.missing == 0
    assert world.node.post_count == 1, "reconciliation must never POST"
    assert world.intent().state == DispatchIntentState.CONFIRMED

    # ...and the batch is now stampable: this is the exact lookup
    # `stamp_targets_dispatched` performs before it will write
    # `dispatched_at`/`dispatch_intent_id` onto a target.
    committed = get_committed_dispatch((world.redis, world.store()), world.identity)
    assert committed is not None
    assert committed.jobid == jobid


def test_a_replay_after_reconciliation_returns_the_committed_jobid(
    world: _World,
) -> None:
    store = world.store()
    jobid = str(store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE).scrapyd_job_id)
    world.schedule(world.client(store), jobid)
    assert world.node.post_count == 1

    # An at-least-once redelivery of the same dispatch re-derives the same
    # identity and is answered from the durable row, not from the node.
    world.redis.store.clear()  # the guard aged out; it is not the authority
    again = world.schedule(world.client(world.store()), jobid)

    assert again == jobid
    assert world.node.post_count == 1


def test_missing_on_node_reposts_with_the_same_jobid(world: _World) -> None:
    store = world.store()
    planned = store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    jobid = str(planned.scrapyd_job_id)

    def _die(identity, scrapyd_job_id):
        raise ProcessDied("SIGKILL between the POST and the confirm")

    store.confirm = _die  # type: ignore[method-assign]
    with pytest.raises(ProcessDied):
        world.schedule(world.client(store), jobid)
    assert world.node.post_count == 1

    # The node has no record of it (restarted, or the entry aged out of
    # the 100-deep finished history).
    world.node.forget_all()
    report = reconcile_inflight_intents(world.factory, world.node)

    assert report.missing == 1 and report.confirmed == 0
    assert world.intent().state == DispatchIntentState.RECONCILED_MISSING
    assert world.node.post_count == 1, "the reconciler itself never POSTs"

    # Only now is a re-POST authorized -- and it carries the same name.
    world.redis.store.clear()
    world.schedule(world.client(world.store()), jobid)

    assert world.node.posted_jobids == [jobid, jobid]
    assert world.intent().state == DispatchIntentState.CONFIRMED


def test_an_unreachable_node_is_never_read_as_absence(world: _World) -> None:
    store = world.store()
    jobid = str(store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE).scrapyd_job_id)

    def _die(identity, scrapyd_job_id):
        raise ProcessDied("SIGKILL between the POST and the confirm")

    store.confirm = _die  # type: ignore[method-assign]
    with pytest.raises(ProcessDied):
        world.schedule(world.client(store), jobid)

    world.node.reachable = False
    report = reconcile_inflight_intents(world.factory, world.node)

    assert report.unreachable == 1
    assert report.confirmed == 0 and report.missing == 0
    # The row keeps looking like the open question it is: no verdict, no
    # re-POST authorization, examined again next pass.
    assert world.intent().state == DispatchIntentState.POSTED
