"""Tenant cost-rollup endpoint (EPA C6) — `GET /v1/cost-rollups`.

The tenant-scoped counterpart to `GET /ops/metrics`'s fleet-only
`cost_rollup` section (`apps/api/app/routers/ops_metrics.py`). That
router is explicit that a per-tenant cost breakdown must NEVER be added
there — it is the cross-workspace, service-token-gated, BYPASSRLS
ops-shared surface, and a tenant breakdown on it would be a
confidentiality leak across every other workspace sharing that token.
This router is the opposite posture, on purpose: the ordinary SPEC-03
tenant auth seam (`app.deps.get_current_principal` -> RLS already set on
the yielded session), scope-gated on the narrow, separately-grantable
`Scope.COST_ROLLUPS_READ` (`cost_rollups:read` — see that scope's own
docstring in `app_shared.security.scopes` for why it is not folded into
a broader existing scope), reading `network_cost_rollups` through
`app_shared.repository.scoped_select` with RLS as the second isolation
layer — the same discipline `routers/alerts.py`/`refresh_rules.py`
already follow.

Read-only: this router never writes a rollup row. Rows are populated
entirely by the durable job (`app_shared.netledger.rollups.
run_cost_rollup`, wired on a beat schedule in `apps/scheduler`), never
computed synchronously here — a request against this endpoint costs
exactly one bounded, indexed, keyset-paginated `SELECT`, never a `GROUP
BY` over the raw ledger.

Never imports `apps/workers` (Constitution I) — the rollup job is
enqueued by name elsewhere, same discipline `routers/alerts.py` states
for its own recompute task.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from app_shared.models.network_cost_rollups import NetworkCostRollup
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_select
from app_shared.security.scopes import Scope

from app.deps import Principal, require_scopes
from app.schemas.cost_rollups import CostRollupListResponse, CostRollupResponse

router = APIRouter(tags=["cost-rollups"])


def _invalid_cursor(exc: InvalidCursor) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
    )


@router.get("/v1/cost-rollups", response_model=CostRollupListResponse)
def list_cost_rollups(
    limit: int | None = None,
    cursor: str | None = None,
    rollup_date: date | None = None,
    domain: str | None = None,
    principal_ctx: tuple = Depends(require_scopes(str(Scope.COST_ROLLUPS_READ))),
) -> CostRollupListResponse:
    """`GET /v1/cost-rollups` — this workspace's own bounded daily cost
    buckets, optionally filtered by `rollup_date` and/or `domain`
    (matching the literal `"__other__"` sentinel selects the collapsed
    bucket, same as any other value). Cursor-paginated over
    `(created_at, id)` like every other list endpoint
    (`contracts/pagination.md`)."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(NetworkCostRollup, principal.workspace_id)
    if rollup_date is not None:
        stmt = stmt.where(NetworkCostRollup.rollup_date == rollup_date)
    if domain is not None:
        stmt = stmt.where(NetworkCostRollup.domain == domain)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise _invalid_cursor(exc) from exc
        stmt = stmt.where(keyset_predicate(NetworkCostRollup, after))
    stmt = stmt.order_by(NetworkCostRollup.created_at, NetworkCostRollup.id).limit(
        page_limit + 1
    )

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [CostRollupResponse.model_validate(row) for row in envelope["items"]]
    return CostRollupListResponse(items=items, next_cursor=envelope["next_cursor"])


__all__ = ["router"]
