"""Scope -> active-match resolution (SPEC-13 US2, FR-010/FR-020, research R4;
contracts/job-service-seam.md).

The single scope-aware resolver reused by both the scheduler's refresh
pass (:mod:`app_shared.jobs.service` -- :func:`create_scope_job`) and
any future manual scope-run endpoint (FR-010/FR-011 -- "reuse, don't
duplicate"). Pure query logic over :class:`CompetitorProductMatch`
(sync SQLAlchemy ``Session``, no scrapy/twisted/fastapi) -- always
``status == MatchStatus.ACTIVE`` plus a per-scope predicate, built on
:func:`app_shared.repository.scoped_select` so a bare unscoped query
can never accidentally leak out of this module (Principle II).

A missing/dangling ``target_id`` (deleted competitor/product/variant/
group/match, or one belonging to another workspace) naturally yields an
empty result -- the added scope predicate simply matches zero rows, no
special-cased crash-avoidance needed (FR-020 / spec Edge Cases).
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app_shared.enums import MatchStatus, ScrapeScope
from app_shared.models.catalog import ProductGroupItem
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.repository import scoped_select

__all__ = ["resolve_scope_matches"]


def resolve_scope_matches(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    scope: ScrapeScope,
    target_id: uuid.UUID | None,
) -> list[CompetitorProductMatch]:
    """Return the ACTIVE :class:`CompetitorProductMatch` rows for ``scope``.

    Always ``scoped_select(CompetitorProductMatch, workspace_id).where(status
    == ACTIVE, <scope predicate>)``. Per-scope predicate (research R4):

    * ``WORKSPACE`` -- base-only (every ACTIVE match in the workspace).
    * ``COMPETITOR`` -- ``competitor_id == target_id``.
    * ``PRODUCT`` -- ``product_id == target_id``.
    * ``VARIANT`` -- ``product_variant_id == target_id``.
    * ``MATCH`` -- ``id == target_id``.
    * ``PRODUCT_GROUP`` -- ``EXISTS(product_group_items PGI WHERE
      PGI.product_group_id == target_id AND (PGI.product_id ==
      M.product_id OR PGI.product_variant_id == M.product_variant_id))``
      -- covers both membership arms (a product-arm member pins all
      variants of a product; a variant-arm member pins one variant).

    A ``None``/dangling/cross-workspace ``target_id`` for any
    non-WORKSPACE scope yields ``[]`` -- the predicate simply matches no
    rows, never raising.
    """
    stmt = scoped_select(CompetitorProductMatch, workspace_id).where(
        CompetitorProductMatch.status == MatchStatus.ACTIVE
    )

    if scope is ScrapeScope.WORKSPACE:
        pass
    elif scope is ScrapeScope.COMPETITOR:
        stmt = stmt.where(CompetitorProductMatch.competitor_id == target_id)
    elif scope is ScrapeScope.PRODUCT:
        stmt = stmt.where(CompetitorProductMatch.product_id == target_id)
    elif scope is ScrapeScope.VARIANT:
        stmt = stmt.where(CompetitorProductMatch.product_variant_id == target_id)
    elif scope is ScrapeScope.MATCH:
        stmt = stmt.where(CompetitorProductMatch.id == target_id)
    elif scope is ScrapeScope.PRODUCT_GROUP:
        membership = (
            select(ProductGroupItem.id)
            .where(
                ProductGroupItem.workspace_id == workspace_id,
                ProductGroupItem.product_group_id == target_id,
                or_(
                    and_(
                        ProductGroupItem.product_id.isnot(None),
                        ProductGroupItem.product_id == CompetitorProductMatch.product_id,
                    ),
                    and_(
                        ProductGroupItem.product_variant_id.isnot(None),
                        ProductGroupItem.product_variant_id
                        == CompetitorProductMatch.product_variant_id,
                    ),
                ),
            )
            .correlate(CompetitorProductMatch)
        )
        stmt = stmt.where(exists(membership))
    else:
        raise ValueError(f"unsupported scope {scope!r}")

    return list(session.execute(stmt).scalars().all())
