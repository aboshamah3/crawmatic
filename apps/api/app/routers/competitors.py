"""Competitors endpoints (`contracts/api-competitors.md`) — SPEC-05 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. All reads/writes go
through `app_shared.repository.scoped_select`/`scoped_get` — RLS backs
them as the second isolation layer (FR-002/FR-015), same discipline as
`routers/products.py` / `routers/product_groups.py`.

`default_scrape_profile_id` (create + update) is checked via
`app_shared.profiles.repository.assert_profile_assignable`
(`contracts/assignment-enforcement.md`, SPEC-06 US2 T032): visible
(own-workspace or global) or `None` -> OK; a cross-workspace reference
-> `422 WORKSPACE_MISMATCH`; a dangling id -> `404 NOT_FOUND`.

`POST /v1/competitors` also enforces `app.limits.MAX_DOMAINS_PER_WORKSPACE`
(PLAN §7.4, task-9-brief.md): a workspace may register at most that many
DISTINCT domains -- a cost control, since every registered domain is a
future scrape target. The check counts DISTINCT domains with a bounded,
SQL-side `DISTINCT` projection of just the `domain` column (never a
Python scan of every `Competitor` row); re-registering a domain the
workspace already has is never blocked by this rule (only *new* domains
count against the cap) -- that case is still guarded by the pre-existing
`unique(workspace_id, domain)` -> `409 DUPLICATE_DOMAIN` path below.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.models.competitors_matches import Competitor
from app_shared.models.domain_playbooks import DomainPlaybook
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.profiles.repository import assert_profile_assignable
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.limits import MAX_DOMAINS_PER_WORKSPACE
from app.schemas.competitors import (
    CompetitorCreate,
    CompetitorListResponse,
    CompetitorResponse,
    CompetitorUpdate,
    DeleteOutcome,
)

router = APIRouter(prefix="/v1/competitors", tags=["competitors"])


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _duplicate_domain(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_DOMAIN", "message": message}}
    )


def _workspace_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "WORKSPACE_MISMATCH", "message": message}},
    )


def _domain_limit_reached() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "DOMAIN_LIMIT_REACHED",
                "message": (
                    f"This workspace may track at most {MAX_DOMAINS_PER_WORKSPACE} "
                    "competitor domains. Contact support to raise the limit."
                ),
            }
        },
    )


def _domain_limit_exceeded(
    existing_domains: Sequence[str], new_domain: str, max_domains: int
) -> bool:
    """Pure decision logic for the per-workspace domain cap (PLAN §7.4).

    A domain the workspace already has never counts against the cap
    (re-registration is a no-op for this rule, guarded separately by the
    `unique(workspace_id, domain)` constraint) -- only a genuinely *new*
    domain can push the workspace over `max_domains`.
    """
    return new_domain not in existing_domains and len(existing_domains) >= max_domains


def _check_scrape_profile_assignable(
    session: object, workspace_id: uuid.UUID, profile_id: uuid.UUID | None
) -> None:
    """`assert_profile_assignable` wrapper mapping to the router's own
    `404`/`422` error builders (`contracts/assignment-enforcement.md`)."""
    try:
        assert_profile_assignable(session, workspace_id, profile_id)  # type: ignore[arg-type]
    except MissingReference as exc:
        raise _not_found("Scrape profile not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch(
            "Scrape profile belongs to a different workspace."
        ) from exc


@router.post("", response_model=CompetitorResponse, status_code=201)
def create_competitor(
    payload: CompetitorCreate,
    principal_ctx: tuple = Depends(require_scopes("competitors:write")),
) -> CompetitorResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    _check_scrape_profile_assignable(
        session, principal.workspace_id, payload.default_scrape_profile_id
    )

    # Bounded, SQL-side DISTINCT projection of just `domain` -- never a
    # Python scan of every Competitor row (PLAN §7.4). Checked BEFORE the
    # playbook lookup below: a workspace over its domain cap is rejected
    # outright, so there is no point resolving a profile for it.
    existing_domain_rows = session.execute(
        select(Competitor.domain)
        .where(Competitor.workspace_id == principal.workspace_id)
        .distinct()
    ).all()
    existing_domains = [row[0] for row in existing_domain_rows]
    if _domain_limit_exceeded(existing_domains, payload.domain, MAX_DOMAINS_PER_WORKSPACE):
        raise _domain_limit_reached()

    # 2026-08-11 proxy-cost Fix 4 (PLAN_PROXY_COST_REDUCTION.md): a new
    # competitor for a domain in the curated global playbook inherits the
    # playbook's GLOBAL extraction profile when the caller didn't choose
    # one — every workspace extracts a well-known domain identically,
    # with zero discovery cost. A caller-supplied profile id always wins;
    # an unknown domain (or a playbook row naming a missing global
    # profile) changes nothing.
    default_scrape_profile_id = payload.default_scrape_profile_id
    if default_scrape_profile_id is None:
        playbook_profile_id = session.execute(
            select(ScrapeProfile.id)
            .join(DomainPlaybook, DomainPlaybook.scrape_profile_name == ScrapeProfile.name)
            .where(
                DomainPlaybook.domain == payload.domain,
                ScrapeProfile.workspace_id.is_(None),
            )
        ).scalar_one_or_none()
        if playbook_profile_id is not None:
            default_scrape_profile_id = playbook_profile_id

    competitor = Competitor(
        workspace_id=principal.workspace_id,
        name=payload.name,
        domain=payload.domain,
        status=payload.status,
        legal_status=payload.legal_status,
        robots_policy=payload.robots_policy,
        default_scrape_profile_id=default_scrape_profile_id,
        default_access_policy_id=payload.default_access_policy_id,
        max_concurrent_requests=payload.max_concurrent_requests,
        max_requests_per_minute=payload.max_requests_per_minute,
    )
    session.add(competitor)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_domain(
            "A competitor with this domain already exists in this workspace "
            "(unique(workspace_id, domain))."
        ) from exc

    return CompetitorResponse.model_validate(competitor)


@router.get("", response_model=CompetitorListResponse)
def list_competitors(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("competitors:read")),
) -> CompetitorListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(Competitor, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(Competitor, after))
    stmt = stmt.order_by(Competitor.created_at, Competitor.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [CompetitorResponse.model_validate(c) for c in envelope["items"]]
    return CompetitorListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{competitor_id}", response_model=CompetitorResponse)
def get_competitor(
    competitor_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("competitors:read")),
) -> CompetitorResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    competitor = scoped_get(session, Competitor, competitor_id, principal.workspace_id)
    if competitor is None:
        raise _not_found("Competitor not found.")

    return CompetitorResponse.model_validate(competitor)


@router.patch("/{competitor_id}", response_model=CompetitorResponse)
def update_competitor(
    competitor_id: uuid.UUID,
    payload: CompetitorUpdate,
    principal_ctx: tuple = Depends(require_scopes("competitors:write")),
) -> CompetitorResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    competitor = scoped_get(session, Competitor, competitor_id, principal.workspace_id)
    if competitor is None:
        raise _not_found("Competitor not found.")

    updates = payload.model_dump(exclude_unset=True)
    if "default_scrape_profile_id" in updates:
        _check_scrape_profile_assignable(
            session, principal.workspace_id, updates["default_scrape_profile_id"]
        )
    for field, value in updates.items():
        setattr(competitor, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_domain(
            "A competitor with this domain already exists in this workspace "
            "(unique(workspace_id, domain))."
        ) from exc

    return CompetitorResponse.model_validate(competitor)


@router.delete("/{competitor_id}", response_model=DeleteOutcome)
def delete_competitor(
    competitor_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("competitors:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    competitor = scoped_get(session, Competitor, competitor_id, principal.workspace_id)
    if competitor is None:
        raise _not_found("Competitor not found.")

    # No dependent history exists in this spec (observations/attempts are
    # SPEC-07+) -> hard delete (FR-016). Structured so a future
    # archive-by-status path only needs to swap the branch below for
    # `competitor.status = CompetitorStatus.ARCHIVED`.
    session.delete(competitor)
    session.flush()

    return DeleteOutcome(id=competitor_id, outcome="hard_deleted")
