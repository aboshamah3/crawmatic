"""Matches endpoints (`contracts/api-matches.md`) — SPEC-05 US2 (single-record CRUD).

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. All reads/writes go
through `app_shared.repository.scoped_select`/`scoped_get` — RLS backs
them as the second isolation layer (FR-002/FR-015), same discipline as
`routers/competitors.py`/`routers/variants.py`.

Every `competitor_url` is safety-validated (`app_shared.url_safety`) and
normalized+patterned (`app_shared.url_pattern`) before it is stored — on
create and, when it changes, on update (FR-007/008/009/010/011). Match
references (`competitor_id`, the variant) must resolve within the
caller's workspace — checked via the SPEC-04 workspace-consistency
pre-check (`app_shared.catalog.consistency`) before the composite FKs
would otherwise surface a raw `IntegrityError` (FR-006). `product_id` is
never client-supplied; it is derived from the resolved variant's parent
(research D4).

Bulk-upsert (`POST /v1/matches/bulk-upsert`, US3, `contracts/matches-bulk-upsert.md`)
is a set-based, idempotent upsert on the same 4-column arbiter: every
row's URL is safety-validated + normalized (unsafe rows are reported in
`rejected[]`, not aborting the rest of the batch, FR-013), variants and
competitors are resolved/consistency-checked via bounded scoped lookups
(never per-row), and the whole safe batch lands in exactly **one**
`INSERT ... ON CONFLICT DO UPDATE` (`app_shared.matches.upsert`, SC-006)
that never touches the health fields on re-push.

`scrape_profile_id` (create + update + bulk-upsert) is checked via
`app_shared.profiles.repository.assert_profile_assignable`
(`contracts/assignment-enforcement.md`, SPEC-06 US2 T033): visible
(own-workspace or global) or `None` -> OK; a cross-workspace reference
-> `422 WORKSPACE_MISMATCH`; a dangling id -> `404 NOT_FOUND`. The bulk
path collects the batch's distinct `scrape_profile_id`s and runs **one**
`profile_visibility_map` lookup, never one query per row.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.enums import MatchStatus
from app_shared.matches.upsert import (
    build_matches_upsert,
    dedup_last_wins,
    match_conflict_key,
    prepare_match_urls,
    resolve_match_variants,
    variant_lookup_keys,
)
from app_shared.messaging import enqueue
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.profiles.repository import assert_profile_assignable, profile_visibility_map
from app_shared.repository import scoped_get, scoped_select
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE
from app_shared.url_pattern import derive_match_url_fields
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from app.deps import Principal, require_scopes
from app.schemas.matches import (
    DeleteOutcome,
    MatchBulkUpsertRequest,
    MatchBulkUpsertResult,
    MatchCreate,
    MatchListResponse,
    MatchResponse,
    MatchUpdate,
)

router = APIRouter(prefix="/v1/matches", tags=["matches"])


# --- error builders ----------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _workspace_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "WORKSPACE_MISMATCH", "message": message}},
    )


def _unsafe_url(exc: UnsafeUrlError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "UNSAFE_URL", "message": str(exc), "reason": exc.reason.value}},
    )


def _duplicate_match(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_MATCH", "message": message}}
    )


def _enqueue_price_analysis_recompute(
    *, workspace_id: uuid.UUID, product_variant_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    """Trigger (c), `contracts/recompute-triggers.md` — the comparable set
    changed (a match archived/paused), so the variant's benchmarks/alert
    must be recomputed without waiting for a scrape. `scrape_job_id=None`
    (no job context); enqueue by name only, never `apps/workers`."""
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


def _check_scrape_profile_assignable(
    session: Session, workspace_id: uuid.UUID, profile_id: uuid.UUID | None
) -> None:
    """`assert_profile_assignable` wrapper mapping to this router's own
    `404`/`422` error builders (`contracts/assignment-enforcement.md`)."""
    try:
        assert_profile_assignable(session, workspace_id, profile_id)
    except MissingReference as exc:
        raise _not_found("Scrape profile not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch(
            "Scrape profile belongs to a different workspace."
        ) from exc


def _check_scrape_profile_ids_assignable_bulk(
    session: Session, workspace_id: uuid.UUID, profile_ids: set[uuid.UUID | None]
) -> None:
    """Bulk-path assignability check (`contracts/assignment-enforcement.md`
    "Bulk path (no N+1)"): one `profile_visibility_map` `IN (...)` lookup
    over the batch's distinct `scrape_profile_id`s, then a per-id check
    against the in-memory map — never one query per row."""
    ids = {profile_id for profile_id in profile_ids if profile_id is not None}
    if not ids:
        return

    visibility = profile_visibility_map(session, workspace_id, ids)
    for profile_id in ids:
        if profile_id not in visibility:
            raise _not_found("Scrape profile not found.")
        actual_workspace_id = visibility[profile_id]
        if actual_workspace_id is not None and actual_workspace_id != workspace_id:
            raise _workspace_mismatch(
                "Scrape profile belongs to a different workspace."
            )


# --- reference resolution (Layer 2, contracts/workspace-consistency.md) -----


def _workspace_map(session: Session, model: type, ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """One narrow, unscoped ``id IN (...)`` lookup -> ``{id: workspace_id}``.

    Deliberately unscoped (mirrors `routers/variants.py`
    ``_resolve_parent_product_ids``) so a cross-workspace reference can be
    told apart from a nonexistent one before `assert_refs_in_workspace`
    turns either into a clean `422`/`404` — never a raw `IntegrityError`.
    """
    if not ids:
        return {}
    rows = session.execute(
        select(model.id, model.workspace_id).where(model.id.in_(ids))  # noqa: workspace-scope
    ).all()
    return {row.id: row.workspace_id for row in rows}


def _resolve_competitor(session: Session, workspace_id: uuid.UUID, competitor_id: uuid.UUID) -> None:
    resolved = _workspace_map(session, Competitor, [competitor_id])
    try:
        assert_refs_in_workspace(workspace_id, [competitor_id], resolved)
    except MissingReference as exc:
        raise _not_found("Competitor not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch(
            "Competitor belongs to a different workspace."
        ) from exc


def _resolve_variant(
    session: Session, workspace_id: uuid.UUID, payload: MatchCreate
) -> ProductVariant:
    """Resolve the match's variant in-workspace, yielding `product_variant_id` + `product_id`.

    Exactly one of `product_variant_id`/`variant_external_id`/`variant_sku`
    is set (enforced by the schema). An explicit id is checked via the
    unscoped-lookup + consistency pre-check (distinguishes cross-workspace
    from nonexistent, FR-006); an `external_id`/`sku` lookup is inherently
    workspace-scoped, so a miss there is always `404`.
    """
    if payload.product_variant_id is not None:
        resolved = _workspace_map(session, ProductVariant, [payload.product_variant_id])
        try:
            assert_refs_in_workspace(workspace_id, [payload.product_variant_id], resolved)
        except MissingReference as exc:
            raise _not_found("Variant not found.") from exc
        except CrossWorkspaceReference as exc:
            raise _workspace_mismatch(
                "Variant belongs to a different workspace."
            ) from exc
        variant = scoped_get(session, ProductVariant, payload.product_variant_id, workspace_id)
        assert variant is not None
        return variant

    if payload.variant_external_id:
        stmt = scoped_select(ProductVariant, workspace_id).where(
            ProductVariant.external_id == payload.variant_external_id
        )
        variant = session.execute(stmt).scalars().first()
        if variant is None:
            raise _not_found("Variant not found (variant_external_id).")
        return variant

    stmt = scoped_select(ProductVariant, workspace_id).where(
        ProductVariant.sku == payload.variant_sku
    )
    variant = session.execute(stmt).scalars().first()
    if variant is None:
        raise _not_found("Variant not found (variant_sku).")
    return variant


# --- endpoints -----------------------------------------------------------


@router.post("", response_model=MatchResponse, status_code=201)
def create_match(
    payload: MatchCreate,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    try:
        validate_competitor_url(payload.competitor_url)
    except UnsafeUrlError as exc:
        raise _unsafe_url(exc) from exc
    normalized_url, url_pattern, url_pattern_version = derive_match_url_fields(
        payload.competitor_url
    )

    variant = _resolve_variant(session, ws, payload)
    _resolve_competitor(session, ws, payload.competitor_id)
    _check_scrape_profile_assignable(session, ws, payload.scrape_profile_id)

    match = CompetitorProductMatch(
        workspace_id=ws,
        product_id=variant.product_id,
        product_variant_id=variant.id,
        competitor_id=payload.competitor_id,
        competitor_url=payload.competitor_url,
        normalized_competitor_url=normalized_url,
        url_pattern=url_pattern,
        url_pattern_version=url_pattern_version,
        competitor_variant_identifier=payload.competitor_variant_identifier,
        competitor_variant_sku=payload.competitor_variant_sku,
        competitor_variant_options=payload.competitor_variant_options,
        external_title=payload.external_title,
        scrape_profile_id=payload.scrape_profile_id,
        access_policy_id=payload.access_policy_id,
        priority=payload.priority,
        status=payload.status,
    )
    session.add(match)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_match(
            "A match already exists for this (variant, competitor, normalized "
            "URL) tuple in this workspace "
            "(unique(workspace_id, product_variant_id, competitor_id, "
            "normalized_competitor_url))."
        ) from exc

    return MatchResponse.model_validate(match)


# --- bulk-upsert (US3, contracts/matches-bulk-upsert.md) --------------------


def _resolve_variant_maps(
    session: Session,
    workspace_id: uuid.UUID,
    external_ids: set[str],
    skus: set[str],
    variant_ids: set[uuid.UUID],
) -> tuple[
    dict[str, tuple[uuid.UUID, uuid.UUID]],
    dict[str, tuple[uuid.UUID, uuid.UUID]],
    dict[uuid.UUID, uuid.UUID],
]:
    """One scoped ``IN (...)`` variant lookup covering every identity kind
    named by the batch (`contracts/matches-bulk-upsert.md` "Resolve variants").

    Returns ``(by_external_id, by_sku, by_id)`` -- the first two map a
    variant identity to ``(variant_id, product_id)``; ``by_id`` maps an
    explicit ``product_variant_id`` to its parent ``product_id``. The
    lookup is workspace-scoped (`scoped_select`), so a cross-workspace or
    nonexistent identity simply yields no row -- `resolve_match_variants`
    reports it as unresolved.
    """
    by_external_id: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
    by_sku: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
    by_id: dict[uuid.UUID, uuid.UUID] = {}
    if not (external_ids or skus or variant_ids):
        return by_external_id, by_sku, by_id

    conditions = []
    if external_ids:
        conditions.append(ProductVariant.external_id.in_(external_ids))
    if skus:
        conditions.append(ProductVariant.sku.in_(skus))
    if variant_ids:
        conditions.append(ProductVariant.id.in_(variant_ids))

    rows = (
        session.execute(scoped_select(ProductVariant, workspace_id).where(or_(*conditions)))
        .scalars()
        .all()
    )
    for variant in rows:
        if variant.external_id:
            by_external_id[variant.external_id] = (variant.id, variant.product_id)
        if variant.sku:
            by_sku[variant.sku] = (variant.id, variant.product_id)
        by_id[variant.id] = variant.product_id
    return by_external_id, by_sku, by_id


@router.post("/bulk-upsert", response_model=MatchBulkUpsertResult, status_code=200)
def bulk_upsert_matches(
    payload: MatchBulkUpsertRequest,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> MatchBulkUpsertResult:
    """Set-based bulk upsert (`contracts/matches-bulk-upsert.md`, FR-013, SC-006).

    Flow, per the contract: `prepare_match_urls` (collect `rejected`) ->
    `dedup_last_wins` (in-batch last-wins on the 4-col match key) ->
    resolve variants via one scoped `IN (...)` lookup (unresolved -> a
    single `422`) -> competitor consistency pre-check (one narrow
    `IN (...)` lookup + `assert_refs_in_workspace`) ->
    `build_matches_upsert` executed once. Never a per-row loop.
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if not payload.matches:
        return MatchBulkUpsertResult(upserted=0, matches=[], rejected=[])

    row_dicts = [item.model_dump() for item in payload.matches]

    safe, rejected = prepare_match_urls(row_dicts)
    deduped = list(dedup_last_wins(safe, match_conflict_key))

    external_ids, skus, variant_ids = variant_lookup_keys(deduped)
    by_external_id, by_sku, by_id = _resolve_variant_maps(
        session, ws, external_ids, skus, variant_ids
    )
    resolved, unresolved = resolve_match_variants(
        deduped, by_external_id=by_external_id, by_sku=by_sku, by_id=by_id
    )
    if unresolved:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "UNRESOLVED_VARIANT",
                    "message": (
                        "One or more match rows reference a "
                        "product_variant_id/variant_external_id/variant_sku "
                        "that does not resolve to a variant in this workspace."
                    ),
                    "count": len(unresolved),
                }
            },
        )

    if resolved:
        competitor_ids = {row["competitor_id"] for row in resolved}
        resolved_competitors = _workspace_map(session, Competitor, list(competitor_ids))
        try:
            assert_refs_in_workspace(ws, competitor_ids, resolved_competitors)
        except MissingReference as exc:
            raise _not_found("Competitor not found.") from exc
        except CrossWorkspaceReference as exc:
            raise _workspace_mismatch(
                "Competitor belongs to a different workspace."
            ) from exc

    if not resolved:
        return MatchBulkUpsertResult(upserted=0, matches=[], rejected=rejected)

    _check_scrape_profile_ids_assignable_bulk(
        session, ws, {row.get("scrape_profile_id") for row in resolved}
    )

    final_rows = [
        {
            "workspace_id": ws,
            "product_id": row["product_id"],
            "product_variant_id": row["product_variant_id"],
            "competitor_id": row["competitor_id"],
            "competitor_url": row["competitor_url"],
            "normalized_competitor_url": row["normalized_competitor_url"],
            "url_pattern": row["url_pattern"],
            "url_pattern_version": row["url_pattern_version"],
            "competitor_variant_identifier": row.get("competitor_variant_identifier"),
            "competitor_variant_sku": row.get("competitor_variant_sku"),
            "competitor_variant_options": row.get("competitor_variant_options"),
            "external_title": row.get("external_title"),
            "scrape_profile_id": row.get("scrape_profile_id"),
            "access_policy_id": row.get("access_policy_id"),
            "priority": row.get("priority") or "NORMAL",
            "status": row.get("status") or "ACTIVE",
        }
        for row in resolved
    ]

    stmt = build_matches_upsert(final_rows).returning(CompetitorProductMatch.id)
    match_ids = [row.id for row in session.execute(stmt).all()]
    session.flush()

    matches = (
        session.execute(
            scoped_select(CompetitorProductMatch, ws).where(
                CompetitorProductMatch.id.in_(match_ids)
            )
        )
        .scalars()
        .all()
    )
    return MatchBulkUpsertResult(
        upserted=len(matches),
        matches=[MatchResponse.model_validate(m) for m in matches],
        rejected=rejected,
    )


@router.get("", response_model=MatchListResponse)
def list_matches(
    limit: int | None = None,
    cursor: str | None = None,
    product_variant_id: uuid.UUID | None = None,
    competitor_id: uuid.UUID | None = None,
    principal_ctx: tuple = Depends(require_scopes("matches:read")),
) -> MatchListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(CompetitorProductMatch, principal.workspace_id)
    if product_variant_id is not None:
        stmt = stmt.where(CompetitorProductMatch.product_variant_id == product_variant_id)
    if competitor_id is not None:
        stmt = stmt.where(CompetitorProductMatch.competitor_id == competitor_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(CompetitorProductMatch, after))
    stmt = stmt.order_by(
        CompetitorProductMatch.created_at, CompetitorProductMatch.id
    ).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [MatchResponse.model_validate(m) for m in envelope["items"]]
    return MatchListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{match_id}", response_model=MatchResponse)
def get_match(
    match_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("matches:read")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    return MatchResponse.model_validate(match)


@router.patch("/{match_id}", response_model=MatchResponse)
def update_match(
    match_id: uuid.UUID,
    payload: MatchUpdate,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    prior_status = match.status
    updates = payload.model_dump(exclude_unset=True)

    if "competitor_url" in updates:
        new_url = updates["competitor_url"]
        try:
            validate_competitor_url(new_url)
        except UnsafeUrlError as exc:
            raise _unsafe_url(exc) from exc
        normalized_url, url_pattern, url_pattern_version = derive_match_url_fields(new_url)
        match.competitor_url = new_url
        match.normalized_competitor_url = normalized_url
        match.url_pattern = url_pattern
        match.url_pattern_version = url_pattern_version
        del updates["competitor_url"]

    if "scrape_profile_id" in updates:
        _check_scrape_profile_assignable(
            session, principal.workspace_id, updates["scrape_profile_id"]
        )

    for field, value in updates.items():
        setattr(match, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_match(
            "A match already exists for this (variant, competitor, normalized "
            "URL) tuple in this workspace "
            "(unique(workspace_id, product_variant_id, competitor_id, "
            "normalized_competitor_url))."
        ) from exc

    # SPEC-09 US3 T031 (FR-015, contracts/recompute-triggers.md trigger
    # (c)): the match transitioned into an archived/paused (non-active)
    # status -- the comparable set changed, so recompute the variant's
    # benchmarks/alert without waiting for a scrape. A status PATCH that
    # doesn't change the effective status (e.g. re-PATCHing the same
    # non-active status, or any transition that stays ACTIVE) enqueues
    # nothing.
    if (
        "status" in updates
        and match.status != prior_status
        and match.status != MatchStatus.ACTIVE
    ):
        _enqueue_price_analysis_recompute(
            workspace_id=principal.workspace_id,
            product_variant_id=match.product_variant_id,
            product_id=match.product_id,
        )

    return MatchResponse.model_validate(match)


@router.delete("/{match_id}", response_model=DeleteOutcome)
def delete_match(
    match_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    variant_id = match.product_variant_id
    product_id = match.product_id

    # No dependent history exists in this spec (observations/attempts are
    # SPEC-07+) -> hard delete (FR-016). Structured so a future
    # archive-by-status path only needs to swap the branch below for
    # `match.status = MatchStatus.ARCHIVED`.
    session.delete(match)
    session.flush()

    # SPEC-09 US3 T031 (contracts/recompute-triggers.md trigger (c)): this
    # endpoint is this router's archive path -- deleting a match changes
    # the variant's comparable set, so recompute without waiting for a
    # scrape.
    _enqueue_price_analysis_recompute(
        workspace_id=principal.workspace_id, product_variant_id=variant_id, product_id=product_id
    )

    return DeleteOutcome(id=match_id, outcome="hard_deleted")
