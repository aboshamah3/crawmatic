"""Catalog index routes (`/v1/admin/index`, plan 2026-10-02).

Four routes, all under `/v1/admin` (exempt from the tenant rate limiter,
excluded from the public OpenAPI by the `admin` tag) and all on a static
bearer token. NO tenant API key reaches the index: the SaaS bills
discovery per candidate, and a connector key that could list candidates
for free would bypass that.

Read-only, `require_index_token` (index token OR the SaaS token):

* `GET  /v1/admin/index/status`: the active generation and its counts.
* `POST /v1/admin/index/lookup`: candidates for up to 50 caller-described
  products. Used by the outreach service.

Workspace-addressed, `require_service_token` (the SaaS token only):

* `GET  /v1/admin/index/workspaces/{id}/candidates`: candidates for the
  workspace's own active variants, paged by variant id: at most
  `MAX_VARIANTS_PER_REQUEST` (50) variants are looked up per request,
  `next_cursor` (the last variant id scanned) resumes the rest
  (`docs/contracts/admin-index-candidates.md`). Filters out the
  workspace's own store, anything already matched in ANY status (a
  rejected candidate must not come back and be billed again) and hosts
  beyond the workspace's competitor-domain cap.
* `POST /v1/admin/index/workspaces/{id}/matches`: record chosen
  candidates as PAUSED matches (the SaaS reads PAUSED as "pending
  review"; only ACTIVE matches are ever scraped), creating the competitor
  rows they need. Reject-and-report per row; an existing match is never
  modified.

Sessions are the BYPASSRLS auth session, exactly as `control_plane`: the
caller holds a service token, not a workspace principal, so every
workspace-owned read below carries an explicit `workspace_id` predicate.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app_shared.catalog_index.keys import host_of, store_domain_of_url
from app_shared.catalog_index.lookup import IndexCandidate, IndexQuery, active_generation, lookup
from app_shared.database import get_auth_session
from app_shared.enums import CompetitorStatus, MatchPriority, MatchStatus, VariantStatus
from app_shared.matches.upsert import prepare_match_urls
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import Workspace
from app_shared.repository import scoped_select
from app_shared.url_pattern import derive_match_url_fields
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from app.index_auth import require_index_token
from app.limits import MAX_DOMAINS_PER_WORKSPACE
from app.routers.matches import _bulk_protected_cap_check
from app.schemas.catalog_index import (
    AcceptCandidatesRequest,
    AcceptCandidatesResponse,
    IndexCandidateOut,
    IndexLookupRequest,
    IndexLookupResponse,
    IndexLookupResult,
    IndexStatusResponse,
    RejectedCandidate,
    WorkspaceCandidateOut,
    WorkspaceCandidatesResponse,
)
from app.service_auth import require_service_token

router = APIRouter(prefix="/v1/admin/index", tags=["admin", "catalog-index"])

#: Hard cap on the variants one candidates request looks up (risk review
#: 2026-10-06, P7). Each variant costs up to three index queries; a larger
#: `variant_limit` is clamped, never refused, and `next_cursor` carries the
#: caller to the rest.
MAX_VARIANTS_PER_REQUEST = 50

_MATCH_CONFLICT = ["workspace_id", "product_variant_id", "competitor_id", "normalized_competitor_url"]


def get_catalog_index_session() -> Iterator[Session]:
    """Session seam for the index routes: BYPASSRLS, like the control
    plane's. Its own dependency so a test overrides exactly this router."""
    with get_auth_session() as session:
        yield session
        session.commit()


def _candidate_fields(candidate: IndexCandidate) -> dict:
    return {
        "domain": candidate.domain,
        "host": candidate.host,
        "url": candidate.url,
        "title": candidate.title,
        "brand": candidate.brand,
        "price": candidate.price,
        "currency": candidate.currency,
        "available": candidate.available,
        "observed_at": candidate.observed_at,
        "store_verdict": candidate.store_verdict,
        "stage": candidate.stage,
        "score": candidate.score,
        "matched_key": candidate.matched_key,
    }


def _plan_accept(
    session: Session,
    workspace_id: uuid.UUID,
    candidates: list[tuple[uuid.UUID, str, str | None]],
) -> tuple[list[dict], list[RejectedCandidate], int]:
    """Decide, row by row, what `accept_candidates` would record for
    `(product_variant_id, url, title)` rows: `(rows to insert, rejected,
    competitors created)`. Creates (and flushes) the competitor rows it
    needs, so the candidates route runs it inside a rolled-back savepoint
    as a dry run: a candidate this would reject is never offered, and so
    never billed, again (2026-10-02 review: a PROTECTED_LINK_CAP_REACHED
    row used to come back and be billed on every discovery pass)."""
    rows = [
        {
            "index": index,
            "product_variant_id": variant_id,
            "competitor_url": url,
            "external_title": title,
        }
        for index, (variant_id, url, title) in enumerate(candidates)
    ]
    safe, unsafe = prepare_match_urls(rows)
    rejected = [RejectedCandidate(index=r["index"], code=r["code"], url=r["url"]) for r in unsafe]

    def reject(row: dict, code: str) -> None:
        rejected.append(RejectedCandidate(index=row["index"], code=code, url=row["competitor_url"]))

    variant_products = dict(
        session.execute(
            select(ProductVariant.id, ProductVariant.product_id).where(
                ProductVariant.workspace_id == workspace_id,
                ProductVariant.id.in_({r["product_variant_id"] for r in safe}),
            )
        ).all()
    )
    # Keyed by bare host, so a competitor stored as `www.shop.com` is the
    # same store as an index host `shop.com`.
    competitors: dict[str, Competitor] = {}
    for c in session.execute(scoped_select(Competitor, workspace_id)).scalars():
        competitors.setdefault(host_of(c.domain), c)
    competitors_created = 0
    accepted: list[dict] = []
    for row in safe:
        product_id = variant_products.get(row["product_variant_id"])
        if product_id is None:
            reject(row, "UNRESOLVED_VARIANT")
            continue
        host = host_of(row["competitor_url"])
        competitor = competitors.get(host)
        if competitor is None:
            if len(competitors) >= MAX_DOMAINS_PER_WORKSPACE:
                reject(row, "DOMAIN_LIMIT_REACHED")
                continue
            competitor = Competitor(workspace_id=workspace_id, name=host, domain=host)
            session.add(competitor)
            session.flush()
            competitors[host] = competitor
            competitors_created += 1
        elif competitor.status == CompetitorStatus.ARCHIVED:
            reject(row, "COMPETITOR_ARCHIVED")
            continue
        accepted.append({**row, "product_id": product_id, "competitor_id": competitor.id})

    # The per-product protected-link cap is a cost guard; reuse the bulk
    # upsert's own check (one resolver, never a second classifier). Keep
    # as many rows per product as fit, in order, and reject the rest: the
    # dry run and the real accept then agree on any prefix of a page.
    by_product: dict[uuid.UUID, list[dict]] = defaultdict(list)
    for row in accepted:
        by_product[row["product_id"]].append(row)
    final: list[dict] = []
    for product_rows in by_product.values():
        try:
            _bulk_protected_cap_check(session, workspace_id, product_rows)
            final.extend(product_rows)
            continue
        except HTTPException:
            pass
        fitting: list[dict] = []
        for row in product_rows:
            try:
                _bulk_protected_cap_check(session, workspace_id, [*fitting, row])
            except HTTPException:
                reject(row, "PROTECTED_LINK_CAP_REACHED")
                continue
            fitting.append(row)
        final.extend(fitting)
    return final, rejected, competitors_created


def _require_workspace(session: Session, workspace_id: uuid.UUID) -> None:
    if session.get(Workspace, workspace_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Workspace not found."}},
        )


@router.get("/status", response_model=IndexStatusResponse)
def index_status(
    _: None = Depends(require_index_token),
    session: Session = Depends(get_catalog_index_session),
) -> IndexStatusResponse:
    row = (
        session.execute(
            text(
                "SELECT generation, products, codes, activated_at, source_built_at "
                "FROM catalog_index_loads WHERE status = 'active' "
                "ORDER BY generation DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return IndexStatusResponse(
            generation=None, products=None, codes=None, activated_at=None, source_built_at=None
        )
    return IndexStatusResponse(**row)


@router.post("/lookup", response_model=IndexLookupResponse)
def index_lookup(
    payload: IndexLookupRequest,
    _: None = Depends(require_index_token),
    session: Session = Depends(get_catalog_index_session),
) -> IndexLookupResponse:
    generation = active_generation(session)
    results: list[IndexLookupResult] = []
    for item in payload.products:
        candidates = (
            []
            if generation is None
            else lookup(
                session,
                IndexQuery(
                    title=item.title, brand=item.brand, sku=item.sku, mpn=item.mpn, gtin=item.gtin
                ),
                generation=generation,
                currency=payload.currency,
                exclude_domains=payload.exclude_domains,
                limit=payload.limit_per_product,
                pool_only=payload.pool_only,
            )
        )
        results.append(
            IndexLookupResult(
                ref=item.ref,
                candidates=[IndexCandidateOut(**_candidate_fields(c)) for c in candidates],
            )
        )
    return IndexLookupResponse(generation=generation, results=results)


@router.get("/workspaces/{workspace_id}/candidates", response_model=WorkspaceCandidatesResponse)
def workspace_candidates(
    workspace_id: uuid.UUID,
    max_candidates: int = Query(100, ge=1, le=500),
    variant_limit: int = Query(50, ge=1, le=200),
    per_variant: int = Query(3, ge=1, le=5),
    currency: str = Query("SAR", max_length=8),
    cursor: uuid.UUID | None = Query(None),
    _: None = Depends(require_service_token),
    session: Session = Depends(get_catalog_index_session),
) -> WorkspaceCandidatesResponse:
    _require_workspace(session, workspace_id)
    generation = active_generation(session)
    if generation is None:
        return WorkspaceCandidatesResponse(
            generation=None, candidates=[], next_cursor=None, variants_scanned=0
        )

    variant_limit = min(variant_limit, MAX_VARIANTS_PER_REQUEST)
    stmt = (
        scoped_select(ProductVariant, workspace_id)
        .where(ProductVariant.status == VariantStatus.ACTIVE)
        .order_by(ProductVariant.id)
        .limit(variant_limit)
    )
    if cursor is not None:
        stmt = stmt.where(ProductVariant.id > cursor)
    variants = session.execute(stmt).scalars().all()
    if not variants:
        return WorkspaceCandidatesResponse(
            generation=generation, candidates=[], next_cursor=None, variants_scanned=0
        )

    products = {
        p.id: p
        for p in session.execute(
            scoped_select(Product, workspace_id).where(
                Product.id.in_({v.product_id for v in variants})
            )
        ).scalars()
    }
    # Anything already matched for these variants, in ANY status: an
    # ARCHIVED (rejected) match must not be proposed and billed again.
    existing = {
        (variant_id, url)
        for variant_id, url in session.execute(
            select(
                CompetitorProductMatch.product_variant_id,
                CompetitorProductMatch.normalized_competitor_url,
            ).where(
                CompetitorProductMatch.workspace_id == workspace_id,
                CompetitorProductMatch.product_variant_id.in_([v.id for v in variants]),
            )
        )
    }
    # Keyed by bare host (as `_plan_accept`), so a competitor stored as
    # `www.shop.com` is the same store as an index host `shop.com`.
    competitor_status: dict[str, CompetitorStatus] = {}
    for domain, status in session.execute(
        select(Competitor.domain, Competitor.status).where(Competitor.workspace_id == workspace_id)
    ).all():
        competitor_status.setdefault(host_of(domain), status)
    known_hosts = set(competitor_status)
    # Stores the merchant removed never come back; asked of the lookup
    # itself, so they cannot use up a variant's `per_variant` slots.
    archived_hosts = sorted(
        host for host, status in competitor_status.items() if status == CompetitorStatus.ARCHIVED
    )
    already_matched = Counter(variant_id for variant_id, _url in existing)
    # The workspace's own storefront(s), read off its own product URLs.
    own_domains = sorted(
        {
            store_domain_of_url(url)
            for url in [v.url for v in variants] + [p.url for p in products.values()]
            if url
        }
    )

    out: list[WorkspaceCandidateOut] = []
    scanned = 0
    last_id: uuid.UUID | None = None
    for variant in variants:
        if len(out) >= max_candidates:
            break
        scanned += 1
        last_id = variant.id
        product = products.get(variant.product_id)
        if product is None:
            continue
        found = lookup(
            session,
            IndexQuery(
                title=product.title,
                brand=product.brand,
                sku=variant.sku or product.sku,
                gtin=variant.barcode or product.barcode,
            ),
            generation=generation,
            currency=currency,
            exclude_domains=own_domains + archived_hosts,
            # Over-fetch by the variant's existing matches: those are
            # filtered out below and must not cost it a fresh candidate.
            limit=per_variant + already_matched[variant.id],
        )
        kept = 0
        for candidate in found:
            if len(out) >= max_candidates or kept >= per_variant:
                break
            try:
                validate_competitor_url(candidate.url)
            except UnsafeUrlError:
                continue
            if (variant.id, derive_match_url_fields(candidate.url)[0]) in existing:
                continue
            if competitor_status.get(candidate.host) == CompetitorStatus.ARCHIVED:
                continue  # the merchant removed this competitor
            if candidate.host not in known_hosts:
                if len(known_hosts) >= MAX_DOMAINS_PER_WORKSPACE:
                    continue
                known_hosts.add(candidate.host)
            out.append(
                WorkspaceCandidateOut(
                    product_id=variant.product_id,
                    product_variant_id=variant.id,
                    **_candidate_fields(candidate),
                )
            )
            kept += 1

    # Dry-run the accept over this page and drop what it would reject
    # (above all the per-product protected-link cap): the SaaS bills a
    # candidate before it accepts it, so an unacceptable one must never
    # be offered. The savepoint undoes the competitor rows it creates.
    if out:
        savepoint = session.begin_nested()
        try:
            _final, refused, _created = _plan_accept(
                session,
                workspace_id,
                [(c.product_variant_id, c.url, c.title) for c in out],
            )
        finally:
            savepoint.rollback()
        refused_indexes = {r.index for r in refused}
        out = [c for i, c in enumerate(out) if i not in refused_indexes]

    more = scanned < len(variants) or len(variants) == variant_limit
    return WorkspaceCandidatesResponse(
        generation=generation,
        candidates=out,
        next_cursor=last_id if more else None,
        variants_scanned=scanned,
    )


@router.post("/workspaces/{workspace_id}/matches", response_model=AcceptCandidatesResponse)
def accept_candidates(
    workspace_id: uuid.UUID,
    payload: AcceptCandidatesRequest,
    _: None = Depends(require_service_token),
    session: Session = Depends(get_catalog_index_session),
) -> AcceptCandidatesResponse:
    _require_workspace(session, workspace_id)
    final, rejected, competitors_created = _plan_accept(
        session,
        workspace_id,
        [(item.product_variant_id, item.url, item.title) for item in payload.candidates],
    )

    created = 0
    if final:
        stmt = (
            pg_insert(CompetitorProductMatch)
            .values(
                [
                    {
                        "workspace_id": workspace_id,
                        "product_id": row["product_id"],
                        "product_variant_id": row["product_variant_id"],
                        "competitor_id": row["competitor_id"],
                        "competitor_url": row["competitor_url"],
                        "normalized_competitor_url": row["normalized_competitor_url"],
                        "url_pattern": row["url_pattern"],
                        "url_pattern_version": row["url_pattern_version"],
                        "external_title": row.get("external_title"),
                        "priority": MatchPriority.NORMAL,
                        # PAUSED = pending review in the SaaS; never scraped
                        # or billed until a person confirms it to ACTIVE.
                        "status": MatchStatus.PAUSED,
                    }
                    for row in final
                ]
            )
            .on_conflict_do_nothing(index_elements=_MATCH_CONFLICT)
            .returning(CompetitorProductMatch.id)
        )
        created = len(session.execute(stmt).all())
    return AcceptCandidatesResponse(
        created=created,
        competitors_created=competitors_created,
        rejected=sorted(rejected, key=lambda r: r.index),
    )
