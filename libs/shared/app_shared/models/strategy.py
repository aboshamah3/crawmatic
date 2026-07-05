"""Domain strategy optimizer ORM models: domain_strategy_profiles,
strategy_attempt_stats, strategy_discovery_runs (SPEC-12).

Per ``contracts/rls-and-migration.md`` / ``data-model.md`` §2-§4 — three
new tables in two isolation shapes (research D3):

* :class:`DomainStrategyProfile` and :class:`StrategyDiscoveryRun` —
  standard **workspace-owned** tables (``WorkspaceScopedBase``,
  ``workspace_id NOT NULL``), each registered in
  :data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
  :func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
  migration, not here — this module only declares ORM shape.
* :class:`StrategyAttemptStats` — **no** ``workspace_id`` column at all
  (§22 deliberately omits it): isolation is anchored *transitively*
  through its real FK to the workspace-owned parent profile via the new
  :func:`app_shared.models.rls.emit_fk_transitive_rls_policy` in the
  creating migration. **Excluded** from ``WORKSPACE_OWNED_MODELS`` (a
  ``scoped_select``/``scoped_get`` can't scope a column that doesn't
  exist) — queried only joined to its scoped parent profile via
  ``app_shared/strategy/repository.py`` (the SPEC-10 dual-scope
  exclusion precedent, applied here to a *no-workspace-column* table
  rather than a *nullable-workspace-column* one).

``method_name`` on :class:`StrategyAttemptStats` is a plain ``Text``
column, **not** ``enum_column`` (research D1): it holds the reused
``AccessMethod`` values when ``method_type=ACCESS`` and the reused
``ExtractionMethod`` values when ``method_type=EXTRACTION``, validated
app-side against whichever vocabulary ``method_type`` selects — a
single VARCHAR column can't carry two disjoint SQLAlchemy-native enum
types at once.

Workspace-local composite FKs (``(workspace_id, competitor_id) ->
competitors(workspace_id, id)``) on the two workspace-owned tables make
a cross-workspace competitor reference structurally impossible, not
just app-filtered (the SPEC-05 ``competitor_product_matches`` pattern,
research D4). Several convention-generated names for this table set
would exceed Postgres's 63-byte identifier cap, so those use the
explicit ``dsp``/``sas``/``sdr`` shorthands (the ``cpm`` precedent from
``app_shared.models.competitors_matches``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    AccessMethod,
    DiscoveryRunStatus,
    ExtractionMethod,
    MethodType,
    StrategyStatus,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class DomainStrategyProfile(Base, WorkspaceScopedBase, TimestampMixin):
    """``domain_strategy_profiles`` — the learned access+extraction start per
    ``(workspace, competitor, domain, url_pattern)`` (§22, FR-007, FR-027).

    ``preferred_access_method``/``preferred_extraction_method`` (each
    learned **separately**, FR-011) and their ``*_confidence`` twins are
    ``NULL`` until a promotion (US1) or discovery seed (US3) sets them.
    ``recent_failure_count`` increments on a preferred-method failure and
    resets to 0 on a qualifying success (FR-012/FR-020, Clarification
    #2) — the primary rediscovery signal alongside the persisted+pending
    success rate.
    """

    __tablename__ = "domain_strategy_profiles"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "competitor_id",
            "domain",
            "url_pattern",
            name="uq_dsp_ws_competitor_domain_pattern",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_dsp_workspace_competitor_competitors",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_domain_strategy_profiles_workspace_id_workspaces",
        ),
        # Version-guarded consumption lookup (D6): resolve_strategy_start
        # never mixes a stale url_pattern_version into a fresh lookup.
        Index(
            "ix_dsp_ws_competitor_domain_pattern_version",
            "workspace_id",
            "competitor_id",
            "domain",
            "url_pattern",
            "url_pattern_version",
        ),
    )

    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[StrategyStatus] = enum_column(
        StrategyStatus, nullable=False, default=StrategyStatus.DISCOVERY_REQUIRED
    )
    preferred_access_method: Mapped[AccessMethod | None] = enum_column(
        AccessMethod, nullable=True
    )
    preferred_extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    access_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    confirmed_success_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    recent_failure_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    last_discovery_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class StrategyAttemptStats(Base, TimestampMixin):
    """``strategy_attempt_stats`` — per-method rolled-up counters (§22, FR-009).

    **No** ``WorkspaceScopedBase`` — deliberately no ``workspace_id``
    column (research D3); isolation is transitive via
    ``domain_strategy_profile_id`` -> the workspace-owned parent
    profile. ``method_name`` is a plain ``Text`` column (D1) validated
    app-side against ``AccessMethod``/``ExtractionMethod`` depending on
    ``method_type``. Maintained **only** at flush (US5) via a single
    ``count = count + delta`` UPSERT — never a per-attempt write.
    """

    __tablename__ = "strategy_attempt_stats"
    __table_args__ = (
        UniqueConstraint(
            "domain_strategy_profile_id",
            "method_type",
            "method_name",
            name="uq_sas_profile_method_type_name",
        ),
        ForeignKeyConstraint(
            ["domain_strategy_profile_id"],
            ["domain_strategy_profiles.id"],
            name="fk_sas_profile_id_domain_strategy_profiles",
        ),
    )

    domain_strategy_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    method_type: Mapped[MethodType] = enum_column(MethodType, nullable=False)
    method_name: Mapped[str] = mapped_column(Text(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    success_rate: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=4), nullable=False, default=Decimal("0")
    )
    avg_response_time_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    avg_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class StrategyDiscoveryRun(Base, WorkspaceScopedBase):
    """``strategy_discovery_runs`` — one row per discovery attempt (§22, FR-016..FR-019).

    No unique constraint (data-model §4): multiple discovery runs over
    time for the same ``(workspace, competitor, domain, url_pattern)``
    key are expected (rediscovery re-runs, retried ``NO_WINNER``/
    ``FAILED`` attempts). No ``TimestampMixin`` — only ``created_at``
    (default now) and ``completed_at`` (set on ``COMPLETED``/
    ``NO_WINNER``), per §22.
    """

    __tablename__ = "strategy_discovery_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_sdr_workspace_competitor_competitors",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_strategy_discovery_runs_workspace_id_workspaces",
        ),
        Index(
            "ix_sdr_ws_competitor_domain_pattern",
            "workspace_id",
            "competitor_id",
            "domain",
            "url_pattern",
        ),
    )

    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern: Mapped[str] = mapped_column(Text(), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[DiscoveryRunStatus] = enum_column(
        DiscoveryRunStatus, nullable=False, default=DiscoveryRunStatus.PENDING
    )
    winning_access_method: Mapped[AccessMethod | None] = enum_column(AccessMethod, nullable=True)
    winning_extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
