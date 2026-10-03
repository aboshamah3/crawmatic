"""STARTED targets whose Scrapyd run has ENDED go back at once (2026-09-29, E2.1).

The 2100 s STARTED reaper is age-based: it waits out the longest a live
claimant could legitimately hold a row. When the node itself says the run
that claimed the row is finished (the watchdog killed it, it crashed, the
container was replaced), that wait is pure loss -- the targets sit STARTED
for 35 minutes with nobody working them, and before the per-claim
`started_at` fix a browser claim could sit much longer. The run's own state
on the node it was POSTed to is a direct answer, so it is used directly.

Real SQL (in-memory SQLite) for the same reason as test_jobs_reaper.py: the
WHERE clauses are the safety property.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app_shared.enums import (
    DispatchIntentState,
    MatchPriority,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.reaper import revert_started_targets_of_ended_runs
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL shim
    return "JSON"


NOW = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)
WORKSPACE_ID = uuid.uuid4()
BROWSER_NODE = "http://scrapers-browser:6800"
HTTP_NODE = "http://scrapers:6800"
MIN_AGE = 120


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    ScrapeJob.metadata.create_all(
        engine,
        tables=[ScrapeJob.__table__, ScrapeJobTarget.__table__, DispatchIntent.__table__],
    )
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _job(session: Session) -> ScrapeJob:
    job = ScrapeJob(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=ScrapeJobStatus.RUNNING,
        priority=MatchPriority.NORMAL,
        total_targets=0,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        source=ScrapeJobSource.API,
        created_at=NOW - timedelta(hours=7),
        started_at=NOW - timedelta(hours=7),
    )
    session.add(job)
    session.flush()
    return job


def _intent(session: Session, job: ScrapeJob, *, node_url: str, project: str) -> DispatchIntent:
    intent = DispatchIntent(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        scrape_job_id=job.id,
        planning_generation=0,
        strategy_method="PLAYWRIGHT_DIRECT",
        domain="amazon.sa",
        mode=ScrapeProfileMode.BROWSER,
        node_class=f"{project}:generic_browser_price_spider",
        match_ids_digest="d",
        match_ids=[],
        identity_key=str(uuid.uuid4()),
        identity_payload="{}",
        node_url=node_url,
        scrapyd_job_id=uuid.uuid4(),
        state=DispatchIntentState.CONFIRMED,
        cancellation_generation_at_creation=0,
    )
    session.add(intent)
    session.flush()
    return intent


def _target(
    session: Session,
    job: ScrapeJob,
    intent: DispatchIntent | None,
    *,
    status: ScrapeTargetStatus = ScrapeTargetStatus.STARTED,
    started_seconds_ago: int = 600,
) -> ScrapeJobTarget:
    target = ScrapeJobTarget(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=NOW - timedelta(hours=7),
        started_at=NOW - timedelta(seconds=started_seconds_ago),
        dispatched_at=NOW - timedelta(seconds=started_seconds_ago + 60),
        locked_at=NOW - timedelta(seconds=started_seconds_ago),
        dispatch_intent_id=None if intent is None else intent.id,
    )
    session.add(target)
    session.flush()
    return target


class _Nodes:
    """What each (node, project) lists: {scrapyd_job_id: state}; None = unreachable."""

    def __init__(self, listings: dict[tuple[str, str], dict[str, str] | None]) -> None:
        self.listings = listings
        self.asked: list[tuple[str, str | None]] = []

    def __call__(self, node_url: str, project: str | None) -> dict[str, str] | None:
        self.asked.append((node_url, project))
        return self.listings.get((node_url, project or ""), {})


def _run(session: Session, nodes: _Nodes) -> int:
    return revert_started_targets_of_ended_runs(
        session, now=NOW, run_states=nodes, min_started_age_seconds=MIN_AGE
    )


def test_targets_of_a_finished_run_are_reverted_at_once(session: Session) -> None:
    job = _job(session)
    intent = _intent(session, job, node_url=BROWSER_NODE, project="price_monitor_browser")
    orphans = [_target(session, job, intent) for _ in range(3)]
    nodes = _Nodes(
        {(BROWSER_NODE, "price_monitor_browser"): {str(intent.scrapyd_job_id): "finished"}}
    )

    assert _run(session, nodes) == 3

    for target in orphans:
        session.refresh(target)
        assert target.status is ScrapeTargetStatus.PENDING
        # Same stamp clearing as the age-based reaper: dispatch selects
        # `PENDING AND dispatched_at IS NULL`.
        assert target.dispatched_at is None
        assert target.dispatch_intent_id is None
        assert target.locked_at is None
        assert target.started_at is None
    # One listing per (node, project), however many targets.
    assert nodes.asked == [(BROWSER_NODE, "price_monitor_browser")]


def test_a_run_the_node_no_longer_lists_counts_as_ended(session: Session) -> None:
    """A STARTED row proves its run existed; a node that answers without it
    has restarted or rolled its history -- the run is gone either way."""
    job = _job(session)
    intent = _intent(session, job, node_url=BROWSER_NODE, project="price_monitor_browser")
    target = _target(session, job, intent)

    assert _run(session, _Nodes({(BROWSER_NODE, "price_monitor_browser"): {}})) == 1
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.PENDING


@pytest.mark.parametrize("state", ["running", "pending"])
def test_targets_of_a_live_run_are_left_alone(session: Session, state: str) -> None:
    job = _job(session)
    intent = _intent(session, job, node_url=BROWSER_NODE, project="price_monitor_browser")
    target = _target(session, job, intent, started_seconds_ago=5_000)

    nodes = _Nodes({(BROWSER_NODE, "price_monitor_browser"): {str(intent.scrapyd_job_id): state}})
    assert _run(session, nodes) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.STARTED


def test_an_unreachable_node_is_no_verdict(session: Session) -> None:
    job = _job(session)
    intent = _intent(session, job, node_url=BROWSER_NODE, project="price_monitor_browser")
    target = _target(session, job, intent)

    assert _run(session, _Nodes({(BROWSER_NODE, "price_monitor_browser"): None})) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.STARTED


def test_a_claim_younger_than_the_minimum_age_is_left_alone(session: Session) -> None:
    job = _job(session)
    intent = _intent(session, job, node_url=BROWSER_NODE, project="price_monitor_browser")
    target = _target(session, job, intent, started_seconds_ago=30)

    nodes = _Nodes({(BROWSER_NODE, "price_monitor_browser"): {str(intent.scrapyd_job_id): "finished"}})
    assert _run(session, nodes) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.STARTED


def test_only_started_rows_of_the_ended_run_move(session: Session) -> None:
    job = _job(session)
    ended = _intent(session, job, node_url=HTTP_NODE, project="price_monitor")
    live = _intent(session, job, node_url=HTTP_NODE, project="price_monitor")
    orphan = _target(session, job, ended)
    done = _target(session, job, ended, status=ScrapeTargetStatus.COMPLETED)
    deferred = _target(session, job, ended, status=ScrapeTargetStatus.DEFERRED)
    working = _target(session, job, live)
    no_intent = _target(session, job, None)
    nodes = _Nodes(
        {
            (HTTP_NODE, "price_monitor"): {
                str(ended.scrapyd_job_id): "finished",
                str(live.scrapyd_job_id): "running",
            }
        }
    )

    assert _run(session, nodes) == 1
    for target, expected in [
        (orphan, ScrapeTargetStatus.PENDING),
        (done, ScrapeTargetStatus.COMPLETED),
        (deferred, ScrapeTargetStatus.DEFERRED),
        (working, ScrapeTargetStatus.STARTED),
        (no_intent, ScrapeTargetStatus.STARTED),
    ]:
        session.refresh(target)
        assert target.status is expected
