"""Job-creation service unit tests (SPEC-08 T029, US1, FR-006, FR-010).

`app_shared.jobs.service.create_match_job` — exercised against the
shared `FakeOrmSession` (`tests/unit/_jobs_fake_session.py`) plus a
patched `enqueue` (no real DB/Redis/Celery broker). Per
`contracts/job-service.md`: exactly 1 job + 1 target, correct
provenance (`MANUAL`/`API`/`requested_by`), scope/refs derived from the
match, `total_targets=1`, `status=PENDING`, counters start at 0, and
`enqueue` is called exactly once with the right task name / queue /
kwargs. `create_variant_job` is added by US2 (T034) in this same file.
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
from app_shared.jobs.service import create_match_job
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

from unit._jobs_fake_session import FakeOrmSession


class _FakeEnqueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


@pytest.fixture()
def fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> _FakeEnqueue:
    fake = _FakeEnqueue()
    monkeypatch.setattr(service_module, "enqueue", fake)
    return fake


def _make_match(*, workspace_id: uuid.UUID) -> CompetitorProductMatch:
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    return match


def test_create_match_job_creates_one_job_and_one_target(fake_enqueue: _FakeEnqueue) -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    requested_by = uuid.uuid4()
    match = _make_match(workspace_id=workspace_id)

    job_id, status = create_match_job(
        session, workspace_id=workspace_id, match=match, requested_by=requested_by
    )

    assert status == ScrapeJobStatus.PENDING

    jobs = session._rows.get(ScrapeJob, [])
    targets = session._rows.get(ScrapeJobTarget, [])
    assert len(jobs) == 1
    assert len(targets) == 1

    job = jobs[0]
    target = targets[0]

    assert job.id == job_id
    assert job.workspace_id == workspace_id
    assert job.type == ScrapeJobType.MANUAL
    assert job.source == ScrapeJobSource.API
    assert job.requested_by == requested_by
    assert job.scope == ScrapeScope.MATCH
    assert job.match_id == match.id
    assert job.product_variant_id == match.product_variant_id
    assert job.product_id == match.product_id
    assert job.competitor_id == match.competitor_id
    assert job.total_targets == 1
    assert job.status == ScrapeJobStatus.PENDING
    # Counters are never set by the service — they start at 0 via the
    # ORM column default (`ScrapeJob.success_count`/etc.), only applied
    # by a real INSERT (asserted at the model layer in
    # test_jobs_models.py::test_scrape_job_counters_default_to_zero);
    # they are never assigned in Python here.
    assert getattr(job, "success_count", None) in (None, 0)
    assert getattr(job, "failure_count", None) in (None, 0)
    assert getattr(job, "skipped_count", None) in (None, 0)

    assert target.scrape_job_id == job.id
    assert target.match_id == match.id
    assert target.status == ScrapeTargetStatus.PENDING
    assert target.workspace_id == workspace_id


def test_create_match_job_enqueues_dispatch_exactly_once(fake_enqueue: _FakeEnqueue) -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    match = _make_match(workspace_id=workspace_id)

    job_id, _status = create_match_job(
        session, workspace_id=workspace_id, match=match, requested_by=uuid.uuid4()
    )

    assert len(fake_enqueue.calls) == 1
    call = fake_enqueue.calls[0]
    assert call["name"] == SCRAPE_DISPATCH_JOB
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"] == {
        "scrape_job_id": str(job_id),
        "workspace_id": str(workspace_id),
    }
