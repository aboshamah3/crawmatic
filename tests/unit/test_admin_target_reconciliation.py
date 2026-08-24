from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.routers import admin
from app.schemas.admin import TargetReconciliationRequest


class _Report:
    def __init__(self, workspace_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.workspace_id = workspace_id
        self.job_id = job_id

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "scrape_job_id": str(self.job_id),
            "dry_run": True,
            "candidate_match_ids": [],
            "candidate_count": 0,
            "before": {"success": 2, "failure": 1, "skipped": 0, "total": 3},
            "projected": {"success": 2, "failure": 1, "skipped": 0, "total": 3},
            "job_status_before": "PARTIAL_FAILED",
            "job_status_projected": "PARTIAL_FAILED",
            "applied_count": 0,
        }


def test_admin_reconciliation_returns_a_synchronous_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    def fake_service(session: object, **kwargs: object) -> _Report:
        calls.append({"session": session, **kwargs})
        return _Report(workspace_id, job_id)

    monkeypatch.setattr(admin, "reconcile_successful_failed_targets", fake_service)
    session = object()
    response = admin.reconcile_false_failed_targets(
        workspace_id,
        job_id,
        TargetReconciliationRequest(),
        session,
    )

    assert response.dry_run is True
    assert calls[0]["session"] is session
    assert calls[0]["workspace_id"] == workspace_id


def test_admin_reconciliation_apply_requires_requesting_operator() -> None:
    with pytest.raises(HTTPException) as raised:
        admin.reconcile_false_failed_targets(
            uuid.uuid4(),
            uuid.uuid4(),
            TargetReconciliationRequest(dry_run=False, expected_match_ids=[]),
            object(),
        )
    assert raised.value.status_code == 422
