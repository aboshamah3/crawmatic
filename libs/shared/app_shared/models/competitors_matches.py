"""Competitor/match ORM models: competitors, competitor_product_matches (SPEC-05).

Per ``contracts/models-competitors-matches.md`` / ``data-model.md`` — two
workspace-owned tables, both on
:class:`~app_shared.models.base.WorkspaceScopedBase` (``workspace_id NOT
NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_competitors_matches_tables.py``),
not here — this module only declares ORM shape.

* :class:`Competitor` — ``unique(workspace_id, domain)`` (FR-003, one
  competitor per domain per workspace) **plus** ``unique(workspace_id,
  id)`` so :class:`CompetitorProductMatch` can composite-FK it
  workspace-locally (research D4, same pattern SPEC-04 used for
  ``products``/``product_variants``). Defaults to
  ``legal_status=REVIEW_REQUIRED`` (Constitution Principle VI) and
  ``robots_policy=RESPECT``.
* :class:`CompetitorProductMatch` — exactly one competitor URL linked to
  exactly one ``product_variant`` (and its ``product``, derived from the
  variant's parent, not trusted independently). ``unique(workspace_id,
  product_variant_id, competitor_id, normalized_competitor_url)`` (FR-005)
  is the single conflict arbiter for the bulk upsert (US3); a variant may
  hold unlimited matches, bounded only by this 4-column tuple. Health
  fields (``health_status``, ``consecutive_failures``,
  ``success_rate_7d``, ``current_price_id``, ``last_error_code``,
  ``last_scraped_at``/``last_success_at``/``last_failed_at``) default to
  their FR-017 "nothing scraped yet" state and are never client-settable.
  ``current_price_id``/``access_policy_id`` are plain nullable ``Uuid``
  references with **no** FK (targets SPEC-09/10 don't exist yet).
  ``scrape_profile_id`` (and ``Competitor.default_scrape_profile_id``)
  are promoted to a plain FK -> ``scrape_profiles(id)`` ``ON DELETE SET
  NULL`` by SPEC-06 (added via ``ALTER`` in the SPEC-06 migration).

**Explicit constraint names** (research D5, mirroring the
``product_group_items`` precedent in ``app_shared.models.catalog``): the
SPEC-02 ``NAMING_CONVENTION`` would render names for
``competitor_product_matches``'s 4-column unique and composite FKs well
past Postgres's 63-byte identifier cap, so those names are explicit
``cpm`` (``competitor_product_matches``) shorthands instead. The
``competitors`` table's auto-generated names all fit (<=38 chars) and
stay convention-generated.

All three entity FKs on the match (product / variant / competitor) are
workspace-local composite FKs (``(workspace_id, ref_id) ->
parent(workspace_id, id)``), so a cross-workspace reference is
structurally impossible, not just app-filtered (research D4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    CompetitorStatus,
    HealthStatus,
    LegalStatus,
    MatchPriority,
    MatchStatus,
    RobotsPolicy,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class Competitor(Base, WorkspaceScopedBase, TimestampMixin):
    """``competitors`` — one row per tracked competitor, unique per domain."""

    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "domain"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_competitors_workspace_id_workspaces",
        ),
        # SPEC-06 promotes this nullable column to a plain FK ->
        # scrape_profiles(id) ON DELETE SET NULL (added via ALTER in the
        # SPEC-06 migration, not here). Plain, not composite: a global
        # (workspace_id IS NULL) profile must be assignable by any
        # workspace.
        ForeignKeyConstraint(
            ["default_scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_competitors_default_scrape_profile_id_scrape_profiles",
            ondelete="SET NULL",
        ),
    )

    name: Mapped[str] = mapped_column(Text(), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[CompetitorStatus] = enum_column(
        CompetitorStatus, nullable=False, default=CompetitorStatus.ACTIVE
    )
    legal_status: Mapped[LegalStatus] = enum_column(
        LegalStatus, nullable=False, default=LegalStatus.REVIEW_REQUIRED
    )
    robots_policy: Mapped[RobotsPolicy] = enum_column(
        RobotsPolicy, nullable=False, default=RobotsPolicy.RESPECT
    )
    default_scrape_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    default_access_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    max_concurrent_requests: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_requests_per_minute: Mapped[int | None] = mapped_column(Integer(), nullable=True)


class CompetitorProductMatch(Base, WorkspaceScopedBase, TimestampMixin):
    """``competitor_product_matches`` — one variant <-> one competitor URL.

    NOTE on constraint-name length: Postgres identifiers are capped at
    63 bytes. The fully-spelled-out NAMING_CONVENTION-style names for
    this table's 4-column unique and composite FKs (e.g.
    ``uq_competitor_product_matches_workspace_id_product_variant_id_competitor_id_normalized_competitor_url``,
    ~99 chars) exceed that limit, so these names are explicit and use
    the ``cpm`` (competitor_product_matches) shorthand to stay under 63
    bytes while remaining deterministic and unambiguous (research D5,
    same precedent as ``product_group_items`` in
    ``app_shared.models.catalog``).
    """

    __tablename__ = "competitor_product_matches"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "product_variant_id",
            "competitor_id",
            "normalized_competitor_url",
            name="uq_cpm_ws_variant_competitor_norm_url",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_cpm_workspace_product_products",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_variant_id"],
            ["product_variants.workspace_id", "product_variants.id"],
            name="fk_cpm_workspace_variant_variants",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_cpm_workspace_competitor_competitors",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_cpm_workspace_workspaces",
        ),
        # SPEC-06 promotes this nullable column to a plain FK ->
        # scrape_profiles(id) ON DELETE SET NULL (added via ALTER in the
        # SPEC-06 migration, not here). Plain, not composite: a global
        # (workspace_id IS NULL) profile must be assignable by any
        # workspace.
        ForeignKeyConstraint(
            ["scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_cpm_scrape_profile_id_scrape_profiles",
            ondelete="SET NULL",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    competitor_url: Mapped[str] = mapped_column(Text(), nullable=False)
    normalized_competitor_url: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    competitor_variant_identifier: Mapped[str | None] = mapped_column(Text(), nullable=True)
    competitor_variant_sku: Mapped[str | None] = mapped_column(Text(), nullable=True)
    competitor_variant_options: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    external_title: Mapped[str | None] = mapped_column(Text(), nullable=True)
    scrape_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    access_policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    priority: Mapped[MatchPriority] = enum_column(
        MatchPriority, nullable=False, default=MatchPriority.NORMAL
    )
    status: Mapped[MatchStatus] = enum_column(
        MatchStatus, nullable=False, default=MatchStatus.ACTIVE
    )
    health_status: Mapped[HealthStatus] = enum_column(
        HealthStatus, nullable=False, default=HealthStatus.UNKNOWN
    )
    last_error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    success_rate_7d: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    current_price_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_scraped_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
