"""Job-creation service unit tests (SPEC-08 T029/T034, US1/US2, FR-006, FR-007, FR-010, FR-020).

`app_shared.jobs.service.create_match_job`/`create_variant_job` —
exercised against the shared `FakeOrmSession`
(`tests/unit/_jobs_fake_session.py`) plus a patched `enqueue` (no real
DB/Redis/Celery broker). Per `contracts/job-service.md`:
`create_match_job` -> exactly 1 job + 1 target, correct provenance
(`MANUAL`/`API`/`requested_by`), scope/refs derived from the match,
`total_targets=1`, `status=PENDING`, counters start at 0, `enqueue`
called exactly once with the right task name/queue/kwargs.
`create_variant_job` -> one target per ACTIVE match (inactive
excluded), `total_targets == N`, `scope=VARIANT`, enqueue called once;
zero active matches -> `status=COMPLETED`, `total_targets=0`,
`completed_at` set, enqueue NOT called (US2, T034).
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
from app_shared.jobs.service import create_match_job, create_variant_job
from app_shared.models.catalog import ProductVariant
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


# --- create_variant_job (US2, T034) ------------------------------------------


def _make_variant(*, workspace_id: uuid.UUID, product_id: uuid.UUID | None = None) -> ProductVariant:
    variant = ProductVariant(
        workspace_id=workspace_id,
        product_id=product_id or uuid.uuid4(),
        title="Variant A",
    )
    variant.id = uuid.uuid4()
    return variant


def _make_match_for_variant(
    *,
    workspace_id: uuid.UUID,
    variant_id: uuid.UUID,
    status: MatchStatus = MatchStatus.ACTIVE,
) -> CompetitorProductMatch:
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id,
        competitor_id=uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=status,
    )
    match.id = uuid.uuid4()
    return match


def test_create_variant_job_one_target_per_active_match_inactive_excluded(
    fake_enqueue: _FakeEnqueue,
) -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    requested_by = uuid.uuid4()
    variant = _make_variant(workspace_id=workspace_id)
    active_matches = [
        _make_match_for_variant(workspace_id=workspace_id, variant_id=variant.id)
        for _ in range(3)
    ]
    inactive_match = _make_match_for_variant(
        workspace_id=workspace_id, variant_id=variant.id, status=MatchStatus.PAUSED
    )
    session.seed(*active_matches, inactive_match)

    job_id, status = create_variant_job(
        session, workspace_id=workspace_id, variant=variant, requested_by=requested_by
    )

    assert status == ScrapeJobStatus.PENDING

    jobs = session._rows.get(ScrapeJob, [])
    targets = session._rows.get(ScrapeJobTarget, [])
    assert len(jobs) == 1
    assert len(targets) == 3

    job = jobs[0]
    assert job.id == job_id
    assert job.workspace_id == workspace_id
    assert job.type == ScrapeJobType.MANUAL
    assert job.source == ScrapeJobSource.API
    assert job.requested_by == requested_by
    assert job.scope == ScrapeScope.VARIANT
    assert job.product_variant_id == variant.id
    assert job.product_id == variant.product_id
    assert job.total_targets == 3
    assert job.status == ScrapeJobStatus.PENDING

    target_match_ids = {target.match_id for target in targets}
    assert target_match_ids == {match.id for match in active_matches}
    assert inactive_match.id not in target_match_ids
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


def test_create_variant_job_zero_active_matches_completes_without_enqueue(
    fake_enqueue: _FakeEnqueue,
) -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    variant = _make_variant(workspace_id=workspace_id)
    inactive_match = _make_match_for_variant(
        workspace_id=workspace_id, variant_id=variant.id, status=MatchStatus.ARCHIVED
    )
    session.seed(inactive_match)

    job_id, status = create_variant_job(
        session, workspace_id=workspace_id, variant=variant, requested_by=uuid.uuid4()
    )

    assert status == ScrapeJobStatus.COMPLETED

    jobs = session._rows.get(ScrapeJob, [])
    targets = session._rows.get(ScrapeJobTarget, [])
    assert len(jobs) == 1
    assert targets == []

    job = jobs[0]
    assert job.id == job_id
    assert job.scope == ScrapeScope.VARIANT
    assert job.total_targets == 0
    assert job.status == ScrapeJobStatus.COMPLETED
    assert job.completed_at is not None

    assert fake_enqueue.calls == []
