"""Unit tests for `app_shared.jobs.service.create_scope_job` (SPEC-13 US2
T022, FR-011/FR-012/FR-015, `contracts/job-service-seam.md`).

Exercised against the shared `FakeOrmSession`
(`tests/unit/_jobs_fake_session.py`) plus a patched `enqueue` (no real
DB/Redis/Celery broker) -- mirroring `test_jobs_service.py`'s pattern for
`create_match_job`/`create_variant_job`.

Zero matches -> `(None, None)`, no `ScrapeJob`/`ScrapeJobTarget` created,
`enqueue` never called. >=1 match -> one job + one target per match,
`_enqueue_dispatch` called **before** any commit (the fake session
records ordering via a shared event log so this ordering is asserted
directly, not just inferred).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app_shared.jobs.service as service_module
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.service import create_scope_job
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

from unit._jobs_fake_session import FakeOrmSession


class _EventLoggingSession(FakeOrmSession):
    """`FakeOrmSession` + an ordered event log so enqueue-before-commit
    ordering can be asserted directly (not merely inferred from the fact
    that both happened)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")
        super().commit()


class _FakeEnqueue:
    def __init__(self, session: _EventLoggingSession) -> None:
        self._session = session
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self._session.events.append("enqueue")
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


@pytest.fixture()
def session() -> _EventLoggingSession:
    return _EventLoggingSession()


@pytest.fixture()
def fake_enqueue(
    monkeypatch: pytest.MonkeyPatch, session: _EventLoggingSession
) -> _FakeEnqueue:
    fake = _FakeEnqueue(session)
    monkeypatch.setattr(service_module, "enqueue", fake)
    return fake


def _make_match(
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    product_variant_id: uuid.UUID | None = None,
    status: MatchStatus = MatchStatus.ACTIVE,
) -> CompetitorProductMatch:
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=product_id or uuid.uuid4(),
        product_variant_id=product_variant_id or uuid.uuid4(),
        competitor_id=competitor_id or uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=status,
    )
    match.id = uuid.uuid4()
    return match


def test_zero_matches_returns_none_none_no_job_no_enqueue(
    session: _EventLoggingSession, fake_enqueue: _FakeEnqueue
) -> None:
    workspace_id = uuid.uuid4()
    competitor_id = uuid.uuid4()
    # No matches seeded at all -> resolve_scope_matches yields [].

    job_id, status = create_scope_job(
        session,
        workspace_id=workspace_id,
        scope=ScrapeScope.COMPETITOR,
        target_id=competitor_id,
        requested_by=uuid.uuid4(),
        job_type=ScrapeJobType.SCHEDULED,
        source=ScrapeJobSource.SCHEDULER,
    )

    assert (job_id, status) == (None, None)
    assert session._rows.get(ScrapeJob, []) == []
    assert session._rows.get(ScrapeJobTarget, []) == []
    assert fake_enqueue.calls == []


def test_matches_create_job_and_one_target_each_workspace_scope(
    session: _EventLoggingSession, fake_enqueue: _FakeEnqueue
) -> None:
    workspace_id = uuid.uuid4()
    requested_by = uuid.uuid4()
    matches = [_make_match(workspace_id=workspace_id) for _ in range(3)]
    inactive = _make_match(workspace_id=workspace_id, status=MatchStatus.PAUSED)
    session.seed(*matches, inactive)

    job_id, status = create_scope_job(
        session,
        workspace_id=workspace_id,
        scope=ScrapeScope.WORKSPACE,
        target_id=None,
        requested_by=requested_by,
        job_type=ScrapeJobType.SCHEDULED,
        source=ScrapeJobSource.SCHEDULER,
    )

    assert status == ScrapeJobStatus.PENDING
    jobs = session._rows.get(ScrapeJob, [])
    targets = session._rows.get(ScrapeJobTarget, [])
    assert len(jobs) == 1
    assert len(targets) == 3  # inactive excluded

    job = jobs[0]
    assert job.id == job_id
    assert job.workspace_id == workspace_id
    assert job.type == ScrapeJobType.SCHEDULED
    assert job.source == ScrapeJobSource.SCHEDULER
    assert job.scope == ScrapeScope.WORKSPACE
    assert job.requested_by == requested_by
    assert job.total_targets == 3
    assert job.status == ScrapeJobStatus.PENDING

    target_match_ids = {target.match_id for target in targets}
    assert target_match_ids == {match.id for match in matches}
    assert inactive.id not in target_match_ids
    for target in targets:
        assert target.scrape_job_id == job.id
        assert target.status == ScrapeTargetStatus.PENDING
        assert target.workspace_id == workspace_id

    assert len(fake_enqueue.calls) == 1
    call = fake_enqueue.calls[0]
    assert call["name"] == SCRAPE_DISPATCH_JOB
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"] == {
        "scrape_job_id": str(job_id),
        "workspace_id": str(workspace_id),
    }


def test_enqueue_happens_before_commit(
    session: _EventLoggingSession, fake_enqueue: _FakeEnqueue
) -> None:
    workspace_id = uuid.uuid4()
    session.seed(_make_match(workspace_id=workspace_id))

    create_scope_job(
        session,
        workspace_id=workspace_id,
        scope=ScrapeScope.WORKSPACE,
        target_id=None,
        requested_by=None,
        job_type=ScrapeJobType.SCHEDULED,
        source=ScrapeJobSource.SCHEDULER,
    )

    # create_scope_job itself never commits (the caller owns the
    # transaction) -- so at this point only the "enqueue" event has been
    # recorded; no "commit" event exists yet.
    assert session.events == ["enqueue"]
    assert not session.committed

    # Simulate the caller (e.g. the scheduler's per-rule loop) committing
    # afterward -- enqueue must have already happened before this point.
    session.commit()
    assert session.events == ["enqueue", "commit"]


def test_competitor_scope_resolves_only_matching_competitor(
    session: _EventLoggingSession, fake_enqueue: _FakeEnqueue
) -> None:
    workspace_id = uuid.uuid4()
    target_competitor_id = uuid.uuid4()
    other_competitor_id = uuid.uuid4()
    matching = _make_match(workspace_id=workspace_id, competitor_id=target_competitor_id)
    other = _make_match(workspace_id=workspace_id, competitor_id=other_competitor_id)
    session.seed(matching, other)

    job_id, status = create_scope_job(
        session,
        workspace_id=workspace_id,
        scope=ScrapeScope.COMPETITOR,
        target_id=target_competitor_id,
        requested_by=None,
        job_type=ScrapeJobType.SCHEDULED,
        source=ScrapeJobSource.SCHEDULER,
    )

    assert status == ScrapeJobStatus.PENDING
    targets = session._rows.get(ScrapeJobTarget, [])
    assert len(targets) == 1
    assert targets[0].match_id == matching.id

    job = session._rows.get(ScrapeJob, [])[0]
    assert job.competitor_id == target_competitor_id
    assert job.scope == ScrapeScope.COMPETITOR
