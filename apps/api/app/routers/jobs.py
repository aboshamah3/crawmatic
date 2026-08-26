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

from app_shared.costauth import (
    FLEET_PROVIDER_PROXY,
    AuthorizationPurpose,
    AuthorizationRequest,
    CostAuthorizationDenied,
    CostAuthorizationService,
    DenialReason,
    estimate_bytes,
    estimate_cost_minor_units,
)
from app_shared.jobs.service import create_match_job, create_variant_job
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
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


def cost_authorization_http_error(denial: CostAuthorizationDenied) -> HTTPException:
    """Map a C3 denial onto an HTTP status. Shared by every gated route.

    Two families, and the split is about who can act:

    * ``402 Payment Required`` — entitlement/budget. The caller (or their
      account owner) can fix it.
    * ``409 Conflict`` — fleet state (breaker, domain certification,
      concurrency). Nothing the caller does changes it, and dressing that
      up as a payment problem would send them to the wrong place.
    """
    payment_reasons = {
        DenialReason.ENTITLEMENT_INACTIVE,
        DenialReason.MONEY_BUDGET_EXCEEDED,
        DenialReason.BYTE_BUDGET_EXCEEDED,
        DenialReason.REQUEST_BUDGET_EXCEEDED,
        DenialReason.BROWSER_SECOND_BUDGET_EXCEEDED,
    }
    return HTTPException(
        status_code=402 if denial.reason in payment_reasons else 409,
        detail={
            "error": {
                "code": denial.reason.value,
                "message": denial.detail or denial.reason.value,
            }
        },
    )


def assert_workspace_entitled(workspace_id: uuid.UUID) -> None:
    """The account-level half of the C3 gate, for the FAN-OUT routes.

    ``POST /v1/jobs/run/variant/{id}`` and
    ``POST /v1/variants/{id}/rescrape`` create a job spanning several
    competitor domains, so neither can honestly price or authorize a
    single grant — their paid work is authorized per batch inside
    ``dispatch_job``, which knows each batch's domain and mode and is
    strictly more precise than anything a variant id could decide.

    What they still owe the caller is the one denial that depends on no
    domain and that the caller can act on: an inactive account. Without
    it the WooCommerce plugin's "refresh prices" button answers 202 and
    then dispatches nothing, which is the looks-accepted-never-happens
    outcome this gate exists to remove.
    """
    try:
        CostAuthorizationService(default_workspace_id=workspace_id).assert_entitled()
    except CostAuthorizationDenied as denial:
        raise cost_authorization_http_error(denial) from denial


def _authorize_manual_recheck(
    session, *, workspace_id: uuid.UUID, match: CompetitorProductMatch
) -> None:
    """C3-authorize one on-demand recheck, or raise the matching HTTP error.

    The grant is taken here and **not** released on the way out: the job
    this route is about to create is the work it authorizes, and the grant
    stays live under its lease until the dispatch that fulfils it settles
    it.

    **The reservation carries no ``scrape_job_id``, and therefore exactly
    ONE release path exists: lease expiry + the sweeper.** The job does
    not exist yet at this point — it is created by ``create_match_job``
    *after* this authorization returns, precisely so a denial never leaves
    an orphan job behind — so there is no id to put on the request, and
    ``release_reservations_for_scrape_job`` (cancellation's step 4) cannot
    find this grant by job. If the user cancels, or nothing ever
    dispatches, the hold comes back when the lease lapses and
    ``sweep_expired_reservations`` confirms C1's ledger holds no open
    operation under it — within one maintenance cycle, never longer.

    That is a deliberate trade and this comment used to claim otherwise
    (EPA Phase C F10: it named both release paths, and the job-release one
    was never reachable). The alternative — authorize, create the job,
    then UPDATE the reservation's ``scrape_job_id`` — would make
    cancellation able to return the hold immediately, at the cost of a
    second writer for a column every other path writes exactly once at
    authorize time. For a single-request grant whose hold the sweeper
    already returns on its own, that is not worth a mutable decision
    column; if manual recheck ever grows into multi-request work, revisit
    it there rather than here.

    Denials map to two statuses, and the split is about who can act:

    * ``402 Payment Required`` for the entitlement/budget family — the
      caller (or their account owner) can fix it;
    * ``409 Conflict`` for the fleet-state family (breaker, domain state,
      concurrency) — nothing the caller does changes it, and dressing it
      up as a payment problem would send them to the wrong place.
    """
    domain = ""
    competitor = scoped_get(session, Competitor, match.competitor_id, workspace_id)
    if competitor is not None:
        domain = competitor.domain

    service = CostAuthorizationService(default_workspace_id=workspace_id)
    try:
        service.authorize(
            AuthorizationRequest(
                workspace_id=workspace_id,
                domain=domain,
                transport="PROXY",
                provider=FLEET_PROVIDER_PROXY,
                estimated_bytes=estimate_bytes(1),
                estimated_cost_minor_units=estimate_cost_minor_units(domain, 1),
                purpose=AuthorizationPurpose.MANUAL_RECHECK,
                estimated_requests=1,
                # No dedupe key: a human asking twice means twice.
            )
        )
    except CostAuthorizationDenied as denial:
        raise cost_authorization_http_error(denial) from denial


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

    # EPA C3, paid dispatch site 5 (MANUAL_RECHECK). This is the one
    # authorization site with a HUMAN waiting on it, so a denial becomes
    # an HTTP status rather than a silently-skipped batch: telling the
    # caller "202 accepted" for work the fleet will refuse to dispatch is
    # the failure mode the whole gate exists to remove.
    #
    # The variant route below deliberately has NO gate of its own: it
    # fans out to a job whose batches are each authorized inside
    # `dispatch_job`, one grant per (domain, mode), which is strictly
    # more precise than anything this route could decide from a variant
    # id spanning several competitors.
    _authorize_manual_recheck(session, workspace_id=ws, match=match)

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

    # EPA C3: the account-level half of the gate only — this route fans
    # out across several competitor domains, so per-domain authorization
    # happens per batch in `dispatch_job`. See `assert_workspace_entitled`.
    assert_workspace_entitled(ws)

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
