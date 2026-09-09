"""The `dispatch_intents` state machine, as EPA B2 / F06 redefines it.

`PLANNED -> POSTED -> CONFIRMED | FAILED`, plus `RECONCILED_MISSING` --
the one state from which a re-POST is authorized, and only ever with the
SAME `scrapyd_job_id`.

What is actually asserted here, and why each assertion earns its place:

* **The remote run is named at plan time.** `plan()` writes a
  `scrapyd_job_id` that is `uuid5(SCRAPYD_JOB_ID_NAMESPACE,
  identity.key)` -- derivable by any process from the identity alone,
  identical across two independently-built stores, and *different* for
  work that differs by so much as one match id. That is the property the
  same-id re-POST rests on; without it "re-POST the same job" would be
  "hope the node deduped".
* **Each transition commits in its own short transaction.** The store is
  constructed with a `session_factory`, and the fake session counts
  commits: one per transition, none left open. The five-step protocol
  needs `POSTED` to be *committed* before the network call and the
  network call to run with nothing open, and a store that merely flushed
  would satisfy neither.
* **`mark_missing` refuses to move anything that is not `POSTED`.** A
  `CONFIRMED` intent (the run exists) or a `PLANNED` one (nothing was
  sent) must never be talked into the re-POST state by a sweep that
  raced something else.

Run against `FakeOrmSession` -- no Postgres, no Redis, no Celery. The
column types the migration introduces are exercised in
`tests/integration/test_dispatch_reconcile_fake_scrapyd.py`.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.enums import DispatchIntentState, ScrapeJobStatus
from app_shared.jobs.dispatch_intents import (
    SCRAPYD_JOB_ID_NAMESPACE,
    DispatchIntentStore,
    deterministic_scrapyd_job_id,
)
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.scrapyd.identity import build_dispatch_identity

from unit._jobs_fake_session import FakeOrmSession

_NODE = "http://scrapers-a:6800"


class _CountingSession(FakeOrmSession):
    """`FakeOrmSession` + a commit counter.

    The store's contract in `session_factory` mode is "one committed
    transaction per transition", and a counter is the only way to tell
    that apart from "one flush per transition, committed by somebody
    else later".
    """

    def __init__(self) -> None:
        super().__init__()
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        super().commit()

    def rollback(self) -> None:
        self.rollbacks += 1


def _identity(job_id: uuid.UUID, match_ids: tuple[str, ...] = ("m1", "m2")):
    return build_dispatch_identity(
        scrape_job_id=str(job_id),
        planning_generation=1,
        strategy_method="HTTP/JSONLD/v1",
        domain="shop.example.com",
        mode="HTTP",
        node_class="price_monitor:generic_price_spider",
        match_ids=list(match_ids),
    )


@pytest.fixture()
def world() -> tuple[_CountingSession, uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = _CountingSession()
    job = ScrapeJob(
        workspace_id=workspace_id,
        status=ScrapeJobStatus.RUNNING,
        total_targets=2,
        cancellation_generation=0,
        planning_generation=1,
    )
    job.id = job_id
    session.seed(job)
    return session, workspace_id, job_id


def _store(
    session: _CountingSession, workspace_id: uuid.UUID, job_id: uuid.UUID
) -> DispatchIntentStore:
    @contextmanager
    def factory() -> Iterator[FakeOrmSession]:
        yield session

    return DispatchIntentStore(
        None,
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        session_factory=factory,
    )


def _row(session: FakeOrmSession) -> DispatchIntent:
    rows = session._rows[DispatchIntent]  # noqa: SLF001 - the fake's storage
    assert len(rows) == 1, f"expected exactly one intent, got {len(rows)}"
    return rows[0]


def test_plan_names_the_remote_run_deterministically(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)

    store = _store(session, workspace_id, job_id)
    intent = store.plan(identity, match_ids=("m1", "m2"), node_url=_NODE)

    assert intent.state == DispatchIntentState.PLANNED
    assert intent.node_url == _NODE
    assert intent.scrapyd_job_id == uuid.uuid5(
        SCRAPYD_JOB_ID_NAMESPACE, identity.key
    )
    # Derivable without the row -- which is what lets recovery name the
    # run it is looking for.
    assert intent.scrapyd_job_id == deterministic_scrapyd_job_id(identity)


def test_the_same_identity_always_gets_the_same_jobid_and_different_work_does_not(
    world,
) -> None:
    _session, _workspace_id, job_id = world
    same_a = deterministic_scrapyd_job_id(_identity(job_id, ("m1", "m2")))
    same_b = deterministic_scrapyd_job_id(_identity(job_id, ("m2", "m1")))
    other = deterministic_scrapyd_job_id(_identity(job_id, ("m1", "m2", "m3")))

    # Work order is not work identity (the digest is order-insensitive).
    assert same_a == same_b
    # One extra match is different work, and must not be able to claim
    # the first batch's remote name.
    assert other != same_a


def test_plan_is_get_or_create_and_never_re_mints_the_jobid(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)

    first = store.plan(identity, match_ids=("m1", "m2"), node_url=_NODE)
    second = store.plan(identity, match_ids=("m1", "m2"), node_url=_NODE)

    assert first is second
    assert len(session._rows[DispatchIntent]) == 1  # noqa: SLF001


def test_each_transition_commits_its_own_short_transaction(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)

    store.plan(identity, match_ids=("m1", "m2"), node_url=_NODE)
    assert session.commits == 1, "the plan must be durable before anything is sent"

    intent_id = store.record_post(identity, node_url=_NODE)
    assert session.commits == 2, "POSTED must be committed BEFORE the POST"
    row = _row(session)
    assert str(row.id) == intent_id
    assert row.state == DispatchIntentState.POSTED
    assert row.posted_at is not None

    store.confirm(identity, str(row.scrapyd_job_id))
    assert session.commits == 3
    assert _row(session).state == DispatchIntentState.CONFIRMED
    assert _row(session).confirmed_at is not None


def test_confirm_keeps_the_planned_id_when_a_node_answers_a_non_uuid(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)
    planned = store.plan(identity, match_ids=("m1",), node_url=_NODE).scrapyd_job_id
    store.record_post(identity, node_url=_NODE)

    # A node that ignored the `jobid` field and answered something that is
    # not storable in a uuid column. The column is the NAME of the run, so
    # keeping the name we POSTed under beats storing nothing.
    store.confirm(identity, "node-minted-not-a-uuid")

    row = _row(session)
    assert row.state == DispatchIntentState.CONFIRMED
    assert row.scrapyd_job_id == planned


def test_confirm_adopts_a_node_minted_uuid(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)
    store.plan(identity, match_ids=("m1",), node_url=_NODE)
    store.record_post(identity, node_url=_NODE)

    # Scrapyd 1.6 mints `uuid1().hex` when it is not given a jobid --
    # 32 hex chars, no dashes, which IS a uuid.
    answered = uuid.uuid1().hex
    store.confirm(identity, answered)

    assert _row(session).scrapyd_job_id == uuid.UUID(answered)


def test_fail_records_the_attempt_without_losing_the_name(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)
    planned = store.plan(identity, match_ids=("m1",), node_url=_NODE).scrapyd_job_id
    store.record_post(identity, node_url=_NODE)

    store.fail(identity, "ConnectionError: scrapyd unreachable")

    row = _row(session)
    assert row.state == DispatchIntentState.FAILED
    assert "scrapyd unreachable" in row.error_message
    # "Tried and failed" keeps its name: a retry re-derives the same id.
    assert row.scrapyd_job_id == planned


def test_mark_missing_only_moves_a_posted_intent(world) -> None:
    session, workspace_id, job_id = world
    identity = _identity(job_id)
    store = _store(session, workspace_id, job_id)
    store.plan(identity, match_ids=("m1",), node_url=_NODE)

    # PLANNED: nothing was sent, so there is nothing to declare missing.
    store.mark_missing(identity, "node says no")
    assert _row(session).state == DispatchIntentState.PLANNED

    store.record_post(identity, node_url=_NODE)
    # R11: POSTED alone is no longer enough. `mark_missing` authorizes a
    # re-POST -- a second Scrapyd run and a second cost reservation --
    # so it now refuses unless the row's own absence ledger says the
    # denial was corroborated. A caller that simply asserts "it's gone"
    # is exactly the pre-R11 sweep, which declared work missing while
    # its `schedule.json` request was still travelling.
    store.mark_missing(identity, "node says no")
    assert _row(session).state == DispatchIntentState.POSTED

    # With the ledger filled in by the only writer that may fill it, the
    # transition goes through.
    _row(session).absent_observations = 2
    store.mark_missing(identity, "node says no")
    assert _row(session).state == DispatchIntentState.RECONCILED_MISSING

    # And a CONFIRMED row is never talked back into a re-POST.
    store.confirm(identity, str(_row(session).scrapyd_job_id))
    _row(session).absent_observations = 2
    store.mark_missing(identity, "a racing sweep")
    assert _row(session).state == DispatchIntentState.CONFIRMED


def test_a_store_needs_a_session_or_a_factory(world) -> None:
    _session, workspace_id, job_id = world
    with pytest.raises(ValueError, match="session_factory"):
        DispatchIntentStore(None, workspace_id=workspace_id, scrape_job_id=job_id)
