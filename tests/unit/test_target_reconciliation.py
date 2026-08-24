"""Dry-run/apply safety tests for the false-failed target reconciliation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app_shared.enums import ScrapeErrorCode, ScrapeJobStatus, ScrapeTargetStatus
from app_shared.jobs import reconciliation as reconciliation_mod
from app_shared.jobs.reconciliation import reconcile_successful_failed_targets
from app_shared.jobs.targets import Counts


class _ScalarResult:
    def __init__(self, *, one: Any = None, many: list[Any] | None = None) -> None:
        self._one = one
        self._many = many or []

    def scalar_one_or_none(self) -> Any:
        return self._one

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._many


class _Session:
    def __init__(self, job: Any, candidates: list[Any]) -> None:
        self._results = [
            _ScalarResult(one=job),
            _ScalarResult(many=candidates),
        ]
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return self._results.pop(0)


def _objects() -> tuple[uuid.UUID, uuid.UUID, Any, list[Any]]:
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=job_id,
        workspace_id=workspace_id,
        status=ScrapeJobStatus.PARTIAL_FAILED,
        total_targets=4,
        success_count=2,
        failure_count=2,
        skipped_count=0,
        completed_at=now,
    )
    candidates = [
        SimpleNamespace(
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.FAILED,
            error_code=ScrapeErrorCode.TIMEOUT,
            completed_at=now,
        ),
        SimpleNamespace(
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.FAILED,
            error_code=ScrapeErrorCode.HTTP_403,
            completed_at=now,
        ),
    ]
    return workspace_id, job_id, job, candidates


def test_dry_run_reports_candidates_and_never_mutates(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, job_id, job, candidates = _objects()
    session = _Session(job, candidates)
    monkeypatch.setattr(
        reconciliation_mod,
        "aggregate_counts",
        lambda *args: Counts(success=2, failure=2, skipped=0, total=4),
    )

    report = reconcile_successful_failed_targets(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job_id,
    )

    assert report.dry_run is True
    assert report.candidate_match_ids == tuple(target.match_id for target in candidates)
    assert report.projected == Counts(success=4, failure=0, skipped=0, total=4)
    assert report.job_status_projected == ScrapeJobStatus.COMPLETED
    assert report.applied_count == 0
    assert all(target.status == ScrapeTargetStatus.FAILED for target in candidates)
    assert job.status == ScrapeJobStatus.PARTIAL_FAILED


def test_apply_requires_exact_reviewed_candidate_set(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, job_id, job, candidates = _objects()
    session = _Session(job, candidates)
    monkeypatch.setattr(
        reconciliation_mod,
        "aggregate_counts",
        lambda *args: Counts(success=2, failure=2, skipped=0, total=4),
    )

    with pytest.raises(ValueError, match="candidate set changed"):
        reconcile_successful_failed_targets(
            session,
            workspace_id=workspace_id,
            scrape_job_id=job_id,
            dry_run=False,
            expected_match_ids=[candidates[0].match_id],
        )

    assert all(target.status == ScrapeTargetStatus.FAILED for target in candidates)


def test_apply_repairs_targets_clears_errors_and_recomputes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, job_id, job, candidates = _objects()
    session = _Session(job, candidates)
    monkeypatch.setattr(
        reconciliation_mod,
        "aggregate_counts",
        lambda *args: Counts(success=2, failure=2, skipped=0, total=4),
    )

    report = reconcile_successful_failed_targets(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        dry_run=False,
        expected_match_ids=[target.match_id for target in candidates],
    )

    assert report.applied_count == 2
    assert all(target.status == ScrapeTargetStatus.COMPLETED for target in candidates)
    assert all(target.error_code is None for target in candidates)
    assert job.success_count == 4
    assert job.failure_count == 0
    assert job.total_targets == 4
    assert job.status == ScrapeJobStatus.COMPLETED
    # Historical completion time is preserved.
    assert job.completed_at == datetime(2026, 8, 24, tzinfo=timezone.utc)


def test_apply_preserves_explicitly_cancelled_job_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, job_id, job, candidates = _objects()
    job.status = ScrapeJobStatus.CANCELLED
    session = _Session(job, candidates)
    monkeypatch.setattr(
        reconciliation_mod,
        "aggregate_counts",
        lambda *args: Counts(success=2, failure=2, skipped=0, total=4),
    )

    report = reconcile_successful_failed_targets(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        dry_run=False,
        expected_match_ids=[target.match_id for target in candidates],
    )

    assert report.job_status_projected == ScrapeJobStatus.CANCELLED
    assert job.status == ScrapeJobStatus.CANCELLED
    assert job.success_count == 4
    assert job.failure_count == 0
    assert all(target.status == ScrapeTargetStatus.COMPLETED for target in candidates)
