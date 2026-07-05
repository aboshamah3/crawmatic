"""Domain strategy optimizer discovery-run endpoints (`contracts/discovery.md`,
`contracts/api-and-observability.md`) — SPEC-12 US3 T028.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. `strategy_discovery_runs`
is tenant-only (`WorkspaceScopedBase`, registered in
`app_shared.repository.WORKSPACE_OWNED_MODELS`) — standard
`scoped_select`/`scoped_get` reads, same discipline as
`routers/domain_access_rules.py`/`routers/jobs.py`.

`POST /v1/strategy/discovery-runs` validates `competitor_id` resolves to
a competitor in the caller's own workspace (mirrors `routers/matches.py`'s
`_resolve_competitor` — 404 dangling/cross-workspace) and delegates
creation + enqueue to `app.services.strategy.create_discovery_run` (this
router never imports `apps/workers`, Principle I). `sample_urls` bounds
(3..10, FR-019) are enforced by `DiscoveryRunCreate`'s Pydantic validator
— out-of-bounds is a `422` before any row is created or task enqueued
(US3 AS2).

`GET /v1/strategy/profiles[/{id}]` + `PATCH .../{id}` (T039) land in the
Polish phase — out of scope here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.models.competitors_matches import Competitor
from app_shared.models.strategy import StrategyDiscoveryRun
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.strategy import DiscoveryRunCreate, DiscoveryRunListResponse, DiscoveryRunResponse
from app.services.strategy import create_discovery_run

router = APIRouter(prefix="/v1/strategy", tags=["strategy"])


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


@router.post("/discovery-runs", response_model=DiscoveryRunResponse, status_code=202)
def create_discovery_run_endpoint(
    payload: DiscoveryRunCreate,
    principal_ctx: tuple = Depends(require_scopes("strategy:write")),
) -> DiscoveryRunResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if scoped_get(session, Competitor, payload.competitor_id, ws) is None:
        raise _not_found("Competitor not found.")

    run = create_discovery_run(
        session,
        workspace_id=ws,
        competitor_id=payload.competitor_id,
        domain=payload.domain,
        url_pattern=payload.url_pattern,
        sample_urls=payload.sample_urls,
    )

    return DiscoveryRunResponse.model_validate(run)


@router.get("/discovery-runs", response_model=DiscoveryRunListResponse)
def list_discovery_runs(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("strategy:read")),
) -> DiscoveryRunListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(StrategyDiscoveryRun, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(StrategyDiscoveryRun, after))
    stmt = stmt.order_by(StrategyDiscoveryRun.created_at, StrategyDiscoveryRun.id).limit(
        page_limit + 1
    )

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [DiscoveryRunResponse.model_validate(r) for r in envelope["items"]]
    return DiscoveryRunListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/discovery-runs/{run_id}", response_model=DiscoveryRunResponse)
def get_discovery_run(
    run_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("strategy:read")),
) -> DiscoveryRunResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    run = scoped_get(session, StrategyDiscoveryRun, run_id, principal.workspace_id)
    if run is None:
        raise _not_found("Discovery run not found.")

    return DiscoveryRunResponse.model_validate(run)
