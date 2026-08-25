"""The lifecycle fix must not weaken mark_target's late-result guard."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.jobs.targets import mark_target


class _Result:
    def __init__(self, target: object) -> None:
        self.target = target

    def scalar_one_or_none(self) -> object:
        return self.target


class _Session:
    def __init__(self, target: object) -> None:
        self.target = target

    def execute(self, statement: object) -> _Result:
        del statement
        return _Result(self.target)


@pytest.mark.parametrize(
    ("existing", "late"),
    [
        (ScrapeTargetStatus.FAILED, ScrapeTargetStatus.COMPLETED),
        (ScrapeTargetStatus.COMPLETED, ScrapeTargetStatus.FAILED),
        (ScrapeTargetStatus.SKIPPED, ScrapeTargetStatus.COMPLETED),
        # EPA A2 (2026-08-25). `CANCELLED` joined the terminal set, so it
        # gets the same guarantee in both directions.
        #
        # Inbound: a spider that was already in flight when a human closed
        # the job must not be able to reopen the target it closed. This is
        # the guard behind the cancellation fence — the fence stops the
        # *observation* from being persisted, and this stops the *status*
        # from being rewritten even if some other path tries.
        (ScrapeTargetStatus.CANCELLED, ScrapeTargetStatus.COMPLETED),
        (ScrapeTargetStatus.CANCELLED, ScrapeTargetStatus.FAILED),
        # Outbound: cancellation never rewrites a real outcome either. A
        # target that already completed or failed keeps its result when a
        # cancellation sweeps the job — which is also what makes a replayed
        # cancellation a no-op rather than a second round of writes.
        (ScrapeTargetStatus.COMPLETED, ScrapeTargetStatus.CANCELLED),
        (ScrapeTargetStatus.FAILED, ScrapeTargetStatus.CANCELLED),
        (ScrapeTargetStatus.SKIPPED, ScrapeTargetStatus.CANCELLED),
    ],
)
def test_late_result_never_reopens_or_reclassifies_terminal_target(
    existing: ScrapeTargetStatus,
    late: ScrapeTargetStatus,
) -> None:
    completed_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
    original_error = (
        ScrapeErrorCode.HTTP_403 if existing == ScrapeTargetStatus.FAILED else None
    )
    target = SimpleNamespace(
        status=existing,
        started_at=completed_at,
        completed_at=completed_at,
        error_code=original_error,
    )

    mark_target(
        _Session(target),
        workspace_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        match_id=uuid.uuid4(),
        status=late,
        error_code=(ScrapeErrorCode.TIMEOUT if late == ScrapeTargetStatus.FAILED else None),
    )

    assert target.status == existing
    assert target.completed_at == completed_at
    assert target.error_code == original_error
