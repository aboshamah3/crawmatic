"""Variants endpoints (`contracts/api-variants.md`) — SPEC-04 US1.

Variants are created via their parent product (`POST /v1/products`) or
bulk-upsert (US2, later); this router exposes read + update only — no
standalone create, no delete (a delete that could orphan a product down
to zero variants is deliberately absent from this feature; see
[analyze F2] note on `PATCH` below).

The one `POST` on a single variant, `/{variant_id}/rescrape`, creates no
variant: it is an *action* on an existing one (trigger a scrape of its
competitor pages now), answering with a job id rather than a variant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.catalog.upsert import plan_upsert
from app_shared.enums import MatchPriority, ScrapeJobStatus, ScrapeScope
from app_shared.jobs.service import create_scope_job
from app_shared.messaging import enqueue
from app_shared.models.alerts import VariantPriceState
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob
from app_shared.models.observations import MatchCurrentPrice
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE

from app.deps import Principal, require_scopes
from app.limits import enforce_batch_cap
from app.schemas.alerts import (
    CompetitorPriceListResponse,
    CompetitorPriceResponse,
    PriceComparisonListResponse,
    PriceComparisonResponse,
)
from app.schemas.catalog import (
    VariantBulkUpsertResult,
    VariantListResponse,
    VariantResponse,
    VariantsBulkUpsertRequest,
    VariantUpdate,
)
from app.schemas.jobs import VariantRescrapeResponse

router = APIRouter(prefix="/v1/variants", tags=["variants"])


def _enqueue_price_analysis_recompute(
    *, workspace_id: uuid.UUID, product_variant_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    """Trigger (b), `contracts/recompute-triggers.md` — enqueue by name only.

    `scrape_job_id=None`: this fires outside any scrape job (a direct
    client price/currency change), so there is no per-job dedup key
    (D7). Never imports `apps/workers` — the seam is
    `app_shared.messaging.enqueue` (Constitution I).
    """
    enqueue(
        PRICE_ANALYSIS_RECOMPUTE,
        queue="price_analysis",
        kwargs={
            "workspace_id": str(workspace_id),
            "product_variant_id": str(product_variant_id),
            "product_id": str(product_id),
            "scrape_job_id": None,
        },
    )


@router.get("", response_model=VariantListResponse)
def list_variants(
    limit: int | None = None,
    cursor: str | None = None,
    product_id: uuid.UUID | None = None,
    principal_ctx: tuple = Depends(require_scopes("variants:read")),
) -> VariantListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(ProductVariant, principal.workspace_id)
    if product_id is not None:
        stmt = stmt.where(ProductVariant.product_id == product_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ProductVariant, after))
    stmt = stmt.order_by(ProductVariant.created_at, ProductVariant.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [VariantResponse.model_validate(v) for v in envelope["items"]]
    return VariantListResponse(items=items, next_cursor=envelope["next_cursor"])


def _price_comparison(price_state: VariantPriceState) -> PriceComparisonResponse:
    """Map a `variant_price_states` row to its API DTO.

    The DTO's `alert_type`/`alert_severity` are the row's
    `latest_alert_type`/`latest_alert_severity` (contract names differ
    from the column names), so this cannot be a plain
    `model_validate(row)` — shared by the per-variant and the bulk list
    route so the two shapes can never drift.
    """
    return PriceComparisonResponse(
        product_variant_id=price_state.product_variant_id,
        client_price=price_state.client_price,
        currency=price_state.currency,
        cheapest_competitor_price=price_state.cheapest_competitor_price,
        average_competitor_price=price_state.average_competitor_price,
        highest_competitor_price=price_state.highest_competitor_price,
        comparable_competitor_count=price_state.comparable_competitor_count,
        alert_type=price_state.latest_alert_type,
        alert_severity=price_state.latest_alert_severity,
        calculated_at=price_state.calculated_at,
    )


# NOTE: this STATIC route must stay registered BEFORE the dynamic
# `/{variant_id}` route below — FastAPI matches routes in registration
# order, and `/{variant_id}` (a `uuid.UUID` path param) would otherwise
# swallow `/price-comparison` and 422 on the un-parseable id. Guarded by
# `test_bulk_price_comparison_is_not_swallowed_by_variant_id_route`
# (tests/unit/test_variants_price_routes.py).
@router.get("/price-comparison", response_model=PriceComparisonListResponse)
def list_price_comparisons(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> PriceComparisonListResponse:
    """`GET /v1/variants/price-comparison` — every analyzed variant's price state.

    The bulk counterpart of `GET /v1/variants/{variant_id}/price-comparison`:
    one keyset-paginated pass over this workspace's `variant_price_states`
    instead of one request per variant. Variants that have never been
    analyzed simply have no row (the same condition the per-variant route
    reports as a 404) — they are absent from the list, never an error.

    Orphaned rows are filtered out: deleting a product hard-deletes its
    `product_variants` but leaves their `variant_price_states` behind
    (there is no FK from `variant_price_states.product_variant_id` to
    `product_variants.id`), so the list is additionally restricted to
    price states whose variant still exists in this workspace. Without it
    this route would surface rows for variants that
    `GET /v1/variants/{variant_id}` 404s.
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(VariantPriceState, principal.workspace_id).where(
        VariantPriceState.product_variant_id.in_(
            select(ProductVariant.id).where(ProductVariant.workspace_id == principal.workspace_id)
        )
    )
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(VariantPriceState, after))
    stmt = stmt.order_by(VariantPriceState.created_at, VariantPriceState.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    return PriceComparisonListResponse(
        items=[_price_comparison(row) for row in envelope["items"]],
        next_cursor=envelope["next_cursor"],
    )


@router.get("/{variant_id}", response_model=VariantResponse)
def get_variant(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("variants:read")),
) -> VariantResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )
    return VariantResponse.model_validate(variant)


@router.get("/{variant_id}/price-comparison", response_model=PriceComparisonResponse)
def get_price_comparison(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> PriceComparisonResponse:
    """`GET /v1/variants/{variant_id}/price-comparison` (SPEC-09 US1, FR-017/FR-020).

    404s an unknown/cross-workspace variant (checked first, via
    `scoped_get`) and, separately, a variant that has never been
    analyzed yet (no `variant_price_states` row) — both distinguishable
    only by message, not status code (contracts/api-alerts.md).
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    price_state = session.execute(
        scoped_select(VariantPriceState, principal.workspace_id).where(
            VariantPriceState.product_variant_id == variant_id
        )
    ).scalar_one_or_none()
    if price_state is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "No price comparison has been computed yet for this variant.",
                }
            },
        )

    return _price_comparison(price_state)


@router.get("/{variant_id}/competitor-prices", response_model=CompetitorPriceListResponse)
def list_competitor_prices(
    variant_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("alerts:read")),
) -> CompetitorPriceListResponse:
    """`GET /v1/variants/{variant_id}/competitor-prices` — the per-competitor
    breakdown behind a variant's price comparison.

    One item per `competitor_product_matches` row of this variant, carrying
    the match's latest known price (its `match_current_prices` row, absent
    until the first successful scrape -> null price fields) and its
    competitor's name. Scope `alerts:read`, matching the sibling
    price-comparison route this feeds (the same client reads both; the
    payload is price-comparison detail, not match management).

    Returns the standard `{items, next_cursor}` envelope
    (`contracts/pagination.md`) keyset-paginated over the *matches*'
    `(created_at, id)` — never a bare JSON array, so a variant matched on
    hundreds of competitors pages like every other list route.

    404 only for an unknown/cross-workspace variant — a variant with no
    matches (or no prices yet) is a legitimate empty `200 {"items": [],
    "next_cursor": null}`, unlike the per-variant price-comparison route
    which 404s a never-analyzed variant.

    Three bounded scoped lookups (one page of matches, then their current
    prices, then their competitors) rather than one three-way SQL join:
    the row set is already capped at `limit + 1` by the keyset page, so
    two extra `id IN (...)` lookups against indexed primary keys are
    cheaper to read and to plan than a join, and the stitch stays in
    Python. (`competitors` *is* a real FK reference from a match —
    `fk_cpm_workspace_competitor_competitors`; only
    `match_current_prices.match_id`/`current_price_id` are genuine soft
    references, §22.)
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    variant = scoped_get(session, ProductVariant, variant_id, ws)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    page_limit = clamp_limit(limit)
    stmt = scoped_select(CompetitorProductMatch, ws).where(
        CompetitorProductMatch.product_variant_id == variant_id
    )
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(CompetitorProductMatch, after))
    stmt = stmt.order_by(CompetitorProductMatch.created_at, CompetitorProductMatch.id).limit(
        page_limit + 1
    )

    envelope = paginate(session.execute(stmt).scalars().all(), page_limit)
    matches = envelope["items"]
    if not matches:
        return CompetitorPriceListResponse(items=[], next_cursor=envelope["next_cursor"])

    prices_by_match_id = {
        row.match_id: row
        for row in session.execute(
            scoped_select(MatchCurrentPrice, ws).where(
                MatchCurrentPrice.match_id.in_([m.id for m in matches])
            )
        )
        .scalars()
        .all()
    }
    names_by_competitor_id = {
        row.id: row.name
        for row in session.execute(
            scoped_select(Competitor, ws).where(
                Competitor.id.in_({m.competitor_id for m in matches})
            )
        )
        .scalars()
        .all()
    }

    items: list[CompetitorPriceResponse] = []
    for match in matches:
        current = prices_by_match_id.get(match.id)
        items.append(
            CompetitorPriceResponse(
                match_id=match.id,
                competitor_id=match.competitor_id,
                # Direct indexing, not `.get(..., "")`: `competitor_id` is a
                # real FK (fk_cpm_workspace_competitor_competitors) within
                # this workspace, so a miss is impossible — and if it ever
                # happened it must fail loudly rather than serve a blank
                # competitor name.
                competitor_name=names_by_competitor_id[match.competitor_id],
                url=match.competitor_url,
                price=current.price if current is not None else None,
                currency=current.currency if current is not None else None,
                scraped_at=current.scraped_at if current is not None else None,
                health_status=match.health_status,
                # 2026-08-09 (problem 4): the availability/outcome pair the
                # plugin needs to tell "unavailable on the competitor's
                # site" apart from "we have no price for this yet" — both
                # None when there is no current-price row at all.
                stock_status=current.stock_status if current is not None else None,
                success=current.success if current is not None else None,
            )
        )
    return CompetitorPriceListResponse(items=items, next_cursor=envelope["next_cursor"])


#: Per-variant rescrape cooldown window. A second rescrape of the same
#: variant inside this window, while the earlier job is still unfinished,
#: is refused (429 `RESCRAPE_COOLDOWN`) instead of pointing a second set
#: of targets at the very same competitor pages — the plugin's refresh
#: button is one click away from being held down.
RESCRAPE_COOLDOWN = timedelta(minutes=10)

#: A job in one of these statuses has not finished yet. The cooldown only
#: bites while an earlier rescrape is genuinely in flight: a job that
#: already reached a terminal status (COMPLETED/PARTIAL_FAILED/FAILED/
#: CANCELLED) inside the window never blocks a fresh one — the user has
#: their prices and may legitimately ask again. Enumerated locally rather
#: than imported from `apps/workers` (`tasks_jobs._NON_TERMINAL_JOB_STATUSES`),
#: which the API must never import (Principle I).
_UNFINISHED_JOB_STATUSES = (ScrapeJobStatus.PENDING, ScrapeJobStatus.RUNNING)


@router.post("/{variant_id}/rescrape", response_model=VariantRescrapeResponse, status_code=202)
def rescrape_variant(
    variant_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("jobs:write")),
) -> VariantRescrapeResponse:
    """`POST /v1/variants/{variant_id}/rescrape` — refresh one variant's
    competitor prices now, instead of waiting for the scheduled sweep.

    The WooCommerce plugin's "refresh prices" action. Creates exactly the
    same rows the scheduler's refresh pass creates — one `scrape_jobs`
    header plus one `scrape_job_targets` row per ACTIVE match of the
    variant — through the same seam
    (`app_shared.jobs.service.create_scope_job`, scope `VARIANT`), which
    enqueues the `scrape_dispatch` Celery task before returning. No new
    queue, no new table: this endpoint is `create_scope_job` plus a
    per-variant cooldown and a match count.

    Scope `jobs:write` — the same gate the sibling
    `POST /v1/jobs/run/variant/{variant_id}` declares, because this
    *is* a job run; a key that may not run jobs may not run this one via
    a different path. (A catalog scope like `variants:write` would let a
    catalog-editing key spend scrape budget.)

    Statuses:

    * `202` — `{"job_id", "match_count"}`. Poll
      `GET /v1/jobs/{job_id}` (scope `jobs:read`) for `status` /
      counters, `GET /v1/jobs/{job_id}/results` for per-target detail,
      then re-read `GET /v1/variants/{variant_id}/competitor-prices`.
    * `404 NOT_FOUND` — unknown or cross-workspace variant.
    * `409 NO_ACTIVE_MATCHES` — the variant has no ACTIVE
      `competitor_product_matches`, so there is nothing to scrape. A
      state conflict, not a malformed request, hence 409 (the
      `CONFLICT` precedent on `PATCH /v1/variants/{variant_id}`) — and
      deliberately *not* the immediately-`COMPLETED` empty job
      `POST /v1/jobs/run/variant/{id}` returns, which would have the
      plugin poll a job that was never going to produce a price.
    * `429 RESCRAPE_COOLDOWN` — an unfinished rescrape of this variant is
      younger than `RESCRAPE_COOLDOWN`; the error carries that job's
      `job_id` (poll it — the answer is already on its way) and a
      `Retry-After` header.

    The cooldown is a plain scoped read over `scrape_jobs` (no Redis, no
    new state): any unfinished `scope=VARIANT` job for this variant
    counts, whichever endpoint created it, so a rescrape can't pile onto
    an operator's in-flight `POST /v1/jobs/run/variant/{id}` either.
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    variant = scoped_get(session, ProductVariant, variant_id, ws)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    now = datetime.now(timezone.utc)
    in_flight = (
        session.execute(
            scoped_select(ScrapeJob, ws).where(
                ScrapeJob.scope == ScrapeScope.VARIANT,
                ScrapeJob.product_variant_id == variant_id,
                ScrapeJob.status.in_(_UNFINISHED_JOB_STATUSES),
                ScrapeJob.created_at >= now - RESCRAPE_COOLDOWN,
            )
        )
        .scalars()
        .all()
    )
    if in_flight:
        # Bounded by the cooldown itself (only jobs created in the last
        # `RESCRAPE_COOLDOWN` and still unfinished can appear here), so the
        # newest is picked in Python rather than with an ORDER BY/LIMIT
        # round trip.
        existing = max(in_flight, key=lambda job: job.created_at)
        retry_after = max(int((existing.created_at + RESCRAPE_COOLDOWN - now).total_seconds()), 1)
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "RESCRAPE_COOLDOWN",
                    "message": (
                        "A rescrape of this variant is already in progress. "
                        "Poll GET /v1/jobs/{job_id} for its result."
                    ),
                    "job_id": str(existing.id),
                }
            },
            headers={"Retry-After": str(retry_after)},
        )

    job_id, _status = create_scope_job(
        session,
        workspace_id=ws,
        scope=ScrapeScope.VARIANT,
        target_id=variant_id,
        requested_by=principal.id,
    )
    if job_id is None:
        # `create_scope_job` creates no job at all when zero ACTIVE
        # matches resolve (FR-015) — nothing to roll back.
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "NO_ACTIVE_MATCHES",
                    "message": (
                        "This variant has no active competitor matches, so there "
                        "are no competitor pages to scrape."
                    ),
                }
            },
        )

    job = scoped_get(session, ScrapeJob, job_id, ws)
    # Just inserted on this session in the same transaction — a miss is
    # impossible and must fail loudly rather than serve a made-up count.
    assert job is not None
    # `scrape_jobs.priority` is the model's existing priority notion
    # (`MatchPriority`), defaulted to NORMAL by `create_scope_job`. Marking
    # a user-triggered rescrape HIGH records the intent on the row; note
    # that no dispatcher currently *orders* by it — promptness comes from
    # the dispatch task being enqueued immediately (same as any manual
    # run), not from queue priority.
    job.priority = MatchPriority.HIGH

    return VariantRescrapeResponse(job_id=job_id, match_count=job.total_targets)


@router.patch("/{variant_id}", response_model=VariantResponse)
def update_variant(
    variant_id: uuid.UUID,
    payload: VariantUpdate,
    principal_ctx: tuple = Depends(require_scopes("variants:write")),
) -> VariantResponse:
    # [analyze F2] No variant-DELETE endpoint exists in this feature, so
    # this PATCH can never drop a product to zero variants — the FR-006
    # last-variant invariant is a structural guard maintained by the
    # catalog service (ensure_at_least_one, unit-tested in T009/T010),
    # not a runtime check here. Deliberately no zero-variant 409 path.
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    variant = scoped_get(session, ProductVariant, variant_id, principal.workspace_id)
    if variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Variant not found."}},
        )

    updates = payload.model_dump(exclude_unset=True)
    # "price" (schema/API name) maps to the "current_price" ORM column.
    if "price" in updates:
        updates["current_price"] = updates.pop("price")
    price_or_currency_changed = "current_price" in updates or "currency" in updates
    for field, value in updates.items():
        setattr(variant, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "CONFLICT",
                    "message": (
                        "This title is already used by another variant of the "
                        "same product (unique(workspace_id, product_id, title))."
                    ),
                }
            },
        ) from exc

    # SPEC-09 US3 T030 (FR-015/FR-016, contracts/recompute-triggers.md
    # trigger (b)): a client price/currency change is reflected immediately
    # -- no waiting for a scrape. A PATCH touching only other fields (e.g.
    # `title`) enqueues nothing.
    if price_or_currency_changed:
        _enqueue_price_analysis_recompute(
            workspace_id=principal.workspace_id,
            product_variant_id=variant.id,
            product_id=variant.product_id,
        )

    return VariantResponse.model_validate(variant)


def _resolve_parent_product_ids(
    session, workspace_id: uuid.UUID, item_dicts: list[dict]
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    """One scoped lookup for external_id/sku parent refs, one narrow id-in(...)
    lookup for explicit `product_id` refs (contracts/catalog-bulk-upsert.md
    "Variant->product resolution").

    Returns `(by_external_id, by_sku, workspace_by_explicit_id)` --
    the last is a `{id: workspace_id}` map (not filtered to this
    workspace) so `app_shared.catalog.consistency.assert_refs_in_workspace`
    can distinguish a cross-workspace `product_id` from a nonexistent one.
    """
    external_ids = {i["product_external_id"] for i in item_dicts if i.get("product_external_id")}
    skus = {i["product_sku"] for i in item_dicts if i.get("product_sku")}
    explicit_ids = {i["product_id"] for i in item_dicts if i.get("product_id") is not None}

    by_external_id: dict[str, uuid.UUID] = {}
    by_sku: dict[str, uuid.UUID] = {}
    if external_ids or skus:
        conditions = []
        if external_ids:
            conditions.append(Product.external_id.in_(external_ids))
        if skus:
            conditions.append(Product.sku.in_(skus))
        rows = session.execute(scoped_select(Product, workspace_id).where(or_(*conditions))).scalars().all()
        for p in rows:
            if p.external_id:
                by_external_id[p.external_id] = p.id
            if p.sku:
                by_sku[p.sku] = p.id

    workspace_by_explicit_id: dict[uuid.UUID, uuid.UUID] = {}
    if explicit_ids:
        # Narrow, fixed-column, id-in(...) lookup limited to exactly the
        # referenced ids -- intentionally workspace-unscoped so a
        # cross-workspace product_id can be told apart from a nonexistent
        # one (Layer 2 of the two-layer model, see consistency.md); every
        # id is then re-checked against `workspace_id` via
        # `assert_refs_in_workspace` before it's trusted.
        rows = session.execute(
            select(Product.id, Product.workspace_id).where(Product.id.in_(explicit_ids))  # noqa: workspace-scope
        ).all()
        workspace_by_explicit_id = {row.id: row.workspace_id for row in rows}

    return by_external_id, by_sku, workspace_by_explicit_id


@router.post("/bulk-upsert", response_model=VariantBulkUpsertResult, status_code=200)
def bulk_upsert_variants(
    payload: VariantsBulkUpsertRequest,
    principal_ctx: tuple = Depends(require_scopes("variants:write")),
) -> VariantBulkUpsertResult:
    """Set-based standalone variant bulk upsert (`contracts/catalog-bulk-upsert.md`).

    Each row names its parent product by `product_id` /
    `product_external_id` / `product_sku`; parent resolution is one
    scoped lookup (never per-row). A cross-workspace or unresolvable
    parent reference is rejected (422) via the workspace-consistency
    pre-check (FR-009) before any upsert statement runs.
    """
    enforce_batch_cap(payload.variants, what="variants")
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if not payload.variants:
        return VariantBulkUpsertResult(upserted=0, variants=[])

    item_dicts = [v.model_dump() for v in payload.variants]
    by_external_id, by_sku, workspace_by_explicit_id = _resolve_parent_product_ids(
        session, ws, item_dicts
    )

    resolved_rows: list[dict] = []
    unresolved: list[dict] = []
    for item in item_dicts:
        product_id: uuid.UUID | None = None
        if item.get("product_id") is not None:
            try:
                assert_refs_in_workspace(ws, [item["product_id"]], workspace_by_explicit_id)
                product_id = item["product_id"]
            except (CrossWorkspaceReference, MissingReference):
                product_id = None
        elif item.get("product_external_id"):
            product_id = by_external_id.get(item["product_external_id"])
        elif item.get("product_sku"):
            product_id = by_sku.get(item["product_sku"])

        if product_id is None:
            unresolved.append(item)
            continue
        item["product_id"] = product_id
        resolved_rows.append(item)

    if unresolved:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "UNRESOLVED_PARENT",
                    "message": (
                        "One or more variant rows reference a product_id/"
                        "product_external_id/product_sku that does not "
                        "resolve to a product in this workspace."
                    ),
                    "count": len(unresolved),
                }
            },
        )

    variant_rows = [
        {
            "workspace_id": ws,
            "product_id": r["product_id"],
            "external_id": r.get("external_id"),
            "sku": r.get("sku"),
            "barcode": r.get("barcode"),
            "title": r["title"],
            "option_values": r.get("option_values"),
            "current_price": r["price"],
            "currency": r["currency"],
            "url": r.get("url"),
            "status": r.get("status") or "active",
        }
        for r in resolved_rows
    ]

    variant_ids: list[uuid.UUID] = []
    for stmt in plan_upsert(variant_rows, is_variant=True):
        stmt = stmt.returning(ProductVariant.id)
        variant_ids.extend(row.id for row in session.execute(stmt).all())
    session.flush()

    variants = (
        session.execute(scoped_select(ProductVariant, ws).where(ProductVariant.id.in_(variant_ids)))
        .scalars()
        .all()
        if variant_ids
        else []
    )

    # SPEC-09 US3 T030 (contracts/recompute-triggers.md trigger (b)): every
    # row in this bulk-upsert batch carries a `current_price`/`currency`
    # (both required fields, see `variant_rows` above), so the simplest
    # correct behavior is to enqueue once per upserted variant --
    # idempotent + low volume (the contract explicitly allows this over
    # diffing prior values).
    for variant in variants:
        _enqueue_price_analysis_recompute(
            workspace_id=ws, product_variant_id=variant.id, product_id=variant.product_id
        )

    return VariantBulkUpsertResult(
        upserted=len(variants), variants=[VariantResponse.model_validate(v) for v in variants]
    )
