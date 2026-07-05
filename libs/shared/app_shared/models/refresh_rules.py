"""Refresh rule ORM model: refresh_rules (SPEC-13 US1).

Per ``data-model.md`` / ``contracts/refresh-rules-api.md`` — one new
workspace-owned table, on :class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed) + :class:`~app_shared.models.base.TimestampMixin`
(``created_at``/``updated_at``), registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_refresh_rules.py``), not here — this
module only declares ORM shape.

:class:`RefreshRule` captures *what* to re-scrape (one of the six
``ScrapeScope`` members + at most one target id, research R6) on *what
cadence* (exactly one of a 5-field UTC ``cron_expression`` or
``interval_minutes``, research R1/R9). Each of the five scope-target
columns (``product_id``/``product_variant_id``/``product_group_id``/
``competitor_id``/``match_id``) is a **nullable workspace-local
composite FK** (``(workspace_id, X) -> table(workspace_id, id)``) with
``ondelete="CASCADE"`` (research R7) — deleting the target deletes the
rules that reference it, so the scheduler pass never dereferences a
missing target and the delete is never blocked. Constraint names for
these five FKs use the ``rr`` (refresh_rules) shorthand (mirrors the
``cpm``/``competitor_product_matches`` precedent in
``app_shared.models.competitors_matches``) to stay under Postgres's
63-byte identifier cap.

Three CHECK constraints (defense-in-depth alongside the Pydantic-layer
validation in ``apps/api/app/schemas/refresh_rules.py``, research R9):
exactly-one-cadence, positive interval, and the scope<->target-id
matrix (WORKSPACE ⇒ all five target ids NULL; every other scope ⇒
exactly its own target id NON-NULL and the rest NULL).

The partial index ``ix_refresh_rules_due`` on ``(next_run_at) WHERE
enabled`` supports the scheduler's due-rule claim query
(``WHERE enabled AND next_run_at <= now() ORDER BY next_run_at``,
research R5, Principle VIII) cheaply at scale.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import ScrapeScope, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class RefreshRule(Base, WorkspaceScopedBase, TimestampMixin):
    """``refresh_rules`` — a workspace's DB-driven "how often to re-scrape" policy.

    ``enabled`` (default ``true``) is the only explicit lifecycle state;
    disabled rules are simply never claimed by the scheduler pass. No
    ``UniqueConstraint(workspace_id, id)`` is added — nothing
    composite-FKs ``refresh_rules`` (autospec-clarify: multiple rules
    per scope/target are intentionally allowed).
    """

    __tablename__ = "refresh_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_refresh_rules_workspace_id_workspaces",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_rr_workspace_product_products",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_variant_id"],
            ["product_variants.workspace_id", "product_variants.id"],
            name="fk_rr_workspace_variant_variants",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_group_id"],
            ["product_groups.workspace_id", "product_groups.id"],
            name="fk_rr_workspace_group_groups",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_rr_workspace_competitor_competitors",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "match_id"],
            ["competitor_product_matches.workspace_id", "competitor_product_matches.id"],
            name="fk_rr_workspace_match_matches",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "num_nonnulls(cron_expression, interval_minutes) = 1",
            name="exactly_one_cadence",
        ),
        CheckConstraint(
            "interval_minutes IS NULL OR interval_minutes > 0",
            name="interval_minutes_positive",
        ),
        CheckConstraint(
            "("
            "scope = 'WORKSPACE' AND product_id IS NULL AND product_variant_id IS NULL "
            "AND product_group_id IS NULL AND competitor_id IS NULL AND match_id IS NULL"
            ") OR ("
            "scope = 'COMPETITOR' AND competitor_id IS NOT NULL AND product_id IS NULL "
            "AND product_variant_id IS NULL AND product_group_id IS NULL AND match_id IS NULL"
            ") OR ("
            "scope = 'PRODUCT' AND product_id IS NOT NULL AND product_variant_id IS NULL "
            "AND product_group_id IS NULL AND competitor_id IS NULL AND match_id IS NULL"
            ") OR ("
            "scope = 'VARIANT' AND product_variant_id IS NOT NULL AND product_id IS NULL "
            "AND product_group_id IS NULL AND competitor_id IS NULL AND match_id IS NULL"
            ") OR ("
            "scope = 'PRODUCT_GROUP' AND product_group_id IS NOT NULL AND product_id IS NULL "
            "AND product_variant_id IS NULL AND competitor_id IS NULL AND match_id IS NULL"
            ") OR ("
            "scope = 'MATCH' AND match_id IS NOT NULL AND product_id IS NULL "
            "AND product_variant_id IS NULL AND product_group_id IS NULL AND competitor_id IS NULL"
            ")",
            name="scope_target",
        ),
        Index(
            "ix_refresh_rules_due",
            "next_run_at",
            postgresql_where=text("enabled"),
        ),
    )

    name: Mapped[str] = mapped_column(Text(), nullable=False)
    scope: Mapped[ScrapeScope] = enum_column(ScrapeScope, nullable=False)

    # Nullable workspace-local composite scope-target FKs (research R7)
    # — set according to `scope`, enforced by the `ck_refresh_rules_scope_target`
    # CHECK above (+ the Pydantic-layer `SCOPE_TARGET_MISMATCH` validator).
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    product_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    product_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    competitor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    # Exactly one of the two is set (`ck_refresh_rules_exactly_one_cadence`
    # + the Pydantic `INVALID_CADENCE`/`INVALID_CRON` validators).
    cron_expression: Mapped[str | None] = mapped_column(Text(), nullable=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    priority: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)

    # Scheduling clock (research R1/R5) — set on create/update
    # (`next_run_at`), advanced per claimed run (all three, research
    # data-model.md "State / lifecycle"). Rollback (crash) leaves all
    # three unchanged so a later pass re-claims (FR-014).
    next_run_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
