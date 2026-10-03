"""A finished job's still-queued Scrapyd runs are cancelled where they live (2026-09-29, E3.3).

Before this, nothing ever removed a job's runs from a node once the job was
over: `cancel.json` existed only on the admin-cancel path and defaulted to
`SCRAPYD_HTTP_URLS[0]` -- never the browser node. After a 12 h deadline the
browser node still held 2,725 pending runs of finished jobs; each one would
spawn a Chromium process only to find its targets terminal and exit.

`purge_live_runs` asks each node a job's intents were POSTed to (one
`listjobs.json` per node and project) and cancels exactly the runs that are
still pending or running, on THAT node, in THAT project.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

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
)
from app_shared.jobs.run_purge import purge_live_runs
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.scrapyd.errors import ScrapydDispatchError


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL shim
    return "JSON"


WORKSPACE_ID = uuid.uuid4()
BROWSER = "http://scrapers-browser:6800"
HTTP = "http://scrapers:6800"
NOW = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    ScrapeJob.metadata.create_all(engine, tables=[ScrapeJob.__table__, DispatchIntent.__table__])
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
        status=ScrapeJobStatus.PARTIAL_FAILED,
        priority=MatchPriority.NORMAL,
        total_targets=0,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        source=ScrapeJobSource.API,
        created_at=NOW,
        started_at=NOW,
    )
    session.add(job)
    session.flush()
    return job


def _intent(session: Session, job: ScrapeJob, node_url: str, project: str) -> DispatchIntent:
    intent = DispatchIntent(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        scrape_job_id=job.id,
        planning_generation=0,
        strategy_method="m",
        domain="amazon.sa",
        mode=ScrapeProfileMode.BROWSER,
        node_class=f"{project}:spider",
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


class _Client:
    def __init__(self, listings: dict[tuple[str, str], dict[str, str] | Exception]) -> None:
        self.listings = listings
        self.cancelled: list[tuple[str, str, str]] = []
        self.listed: list[tuple[str, str | None]] = []

    def list_job_states(self, node_url: str, project: str | None = None) -> dict[str, str]:
        self.listed.append((node_url, project))
        answer = self.listings[(node_url, project or "")]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cancel(self, scrapyd_job_id: str, *, node_url: str | None = None, project: str | None = None) -> bool:
        self.cancelled.append((scrapyd_job_id, node_url or "", project or ""))
        return True


def test_only_live_runs_are_cancelled_on_their_own_node_and_project(session: Session) -> None:
    job = _job(session)
    queued = _intent(session, job, BROWSER, "price_monitor_browser")
    running = _intent(session, job, BROWSER, "price_monitor_browser")
    finished = _intent(session, job, BROWSER, "price_monitor_browser")
    http_queued = _intent(session, job, HTTP, "price_monitor")
    other_job = _intent(session, _job(session), BROWSER, "price_monitor_browser")
    client = _Client(
        {
            (BROWSER, "price_monitor_browser"): {
                str(queued.scrapyd_job_id): "pending",
                str(running.scrapyd_job_id): "running",
                str(finished.scrapyd_job_id): "finished",
                str(other_job.scrapyd_job_id): "pending",
            },
            (HTTP, "price_monitor"): {str(http_queued.scrapyd_job_id): "pending"},
        }
    )

    report = purge_live_runs(
        session, workspace_id=WORKSPACE_ID, scrape_job_id=job.id, client=client
    )

    assert sorted(client.cancelled) == sorted(
        [
            (str(queued.scrapyd_job_id), BROWSER, "price_monitor_browser"),
            (str(running.scrapyd_job_id), BROWSER, "price_monitor_browser"),
            (str(http_queued.scrapyd_job_id), HTTP, "price_monitor"),
        ]
    )
    assert report.cancelled == 3
    assert report.unreachable_nodes == 0
    assert sorted(client.listed) == sorted(
        [(BROWSER, "price_monitor_browser"), (HTTP, "price_monitor")]
    )


def test_an_unreachable_node_is_counted_and_skipped(session: Session) -> None:
    job = _job(session)
    _intent(session, job, BROWSER, "price_monitor_browser")
    client = _Client({(BROWSER, "price_monitor_browser"): ScrapydDispatchError("down")})

    report = purge_live_runs(
        session, workspace_id=WORKSPACE_ID, scrape_job_id=job.id, client=client
    )

    assert client.cancelled == []
    assert report.unreachable_nodes == 1


def test_a_job_with_no_intents_asks_nobody(session: Session) -> None:
    client = _Client({})
    report = purge_live_runs(
        session, workspace_id=WORKSPACE_ID, scrape_job_id=_job(session).id, client=client
    )
    assert client.listed == [] and report.cancelled == 0
