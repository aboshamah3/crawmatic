"""Jobs run/status endpoints (`contracts/api-jobs.md`) — SPEC-08 US1/US2.

Four `/v1` endpoints on the SPEC-03 auth seam
(`app.deps.get_current_principal` -> `set_workspace_context` already
applied to the yielded session), scope-gated via
`app.deps.require_scopes(...)`, all reads through
`app_shared.repository.scoped_select`/`scoped_get` with RLS as the
second isolation layer. Job creation delegates to
`app_shared.jobs.service`; dispatch is enqueued through
`app_shared.messaging` from inside that service call — this router
never imports `apps/workers` (Principle I).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.jobs.service import create_match_job, create_variant_job
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    keyset_predicate,
    paginate,
)
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.jobs import JobResponse, JobResultsResponse, JobRunResponse, JobTargetResponse

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


@router.post("/run/match/{match_id}", response_model=JobRunResponse, status_code=202)
def run_match(
    match_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("jobs:write")),
) -> JobRunResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    match = scoped_get(session, CompetitorProductMatch, match_id, ws)
    if match is None:
        raise _not_found("Match not found.")

    job_id, status = create_match_job(
        session, workspace_id=ws, match=match, requested_by=principal.id
    )

    return JobRunResponse(id=job_id, status=status)


@router.post("/run/variant/{variant_id}", response_model=JobRunResponse, status_code=202)
def run_variant(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("jobs:write")),
) -> JobRunResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    variant = scoped_get(session, ProductVariant, variant_id, ws)
    if variant is None:
        raise _not_found("Variant not found.")

    job_id, status = create_variant_job(
        session, workspace_id=ws, variant=variant, requested_by=principal.id
    )

    return JobRunResponse(id=job_id, status=status)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("jobs:read")),
) -> JobResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    job = scoped_get(session, ScrapeJob, job_id, principal.workspace_id)
    if job is None:
        raise _not_found("Job not found.")

    return JobResponse.model_validate(job)


@router.get("/{job_id}/results", response_model=JobResultsResponse)
def get_job_results(
    job_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("jobs:read")),
) -> JobResultsResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    job = scoped_get(session, ScrapeJob, job_id, ws)
    if job is None:
        raise _not_found("Job not found.")

    page_limit = clamp_limit(limit)
    stmt = scoped_select(ScrapeJobTarget, ws).where(ScrapeJobTarget.scrape_job_id == job.id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ScrapeJobTarget, after))
    stmt = stmt.order_by(ScrapeJobTarget.created_at, ScrapeJobTarget.id).limit(page_limit + 1)

    targets = session.execute(stmt).scalars().all()
    envelope = paginate(targets, page_limit)

    return JobResultsResponse(
        items=[JobTargetResponse.model_validate(target) for target in envelope["items"]],
        next_cursor=envelope["next_cursor"],
    )
