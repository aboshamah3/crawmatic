"""``strategy_method_switches`` ORM model -- the durable, auditable record
of every preferred-method change the strategy optimizer makes, and of
every rollback (EPA W5.5-L2, Item B).

See :mod:`app_shared.strategy.hysteresis` for the full rationale (the
three churn guards this table backs: minimum evidence, the widened
revert band, and rollback) and for every actual read/write, which go
through raw :func:`sqlalchemy.text` statements guarded by a
``to_regclass`` capability probe -- **not** through this model. This
module exists so :data:`app_shared.models.metadata` sees the table for
Alembic autogenerate/offline-render (``target_metadata``); nothing in
the runtime path imports or instantiates
:class:`StrategyMethodSwitch`.

## Shape: workspace-owned, RLS'd

Unlike :class:`app_shared.models.strategy.StrategyAttemptStats` (no
``workspace_id`` column at all, isolated transitively through its FK to
``domain_strategy_profiles``), this table carries its own
``workspace_id`` -- copied from the profile at switch time -- so the
column-based RLS check covers it directly and the flush task's
per-workspace transaction can insert it under the GUC it already set.
RLS via the standard :func:`app_shared.models.rls.emit_rls_policy` in
the creating migration
(``05dfda7cdbb7_rollup_watermark_and_strategy_switch_audit``), not
here -- this module only declares ORM shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKeyConstraint, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime, TimestampMixin, WorkspaceScopedBase


class StrategyMethodSwitch(Base, WorkspaceScopedBase, TimestampMixin):
    """``strategy_method_switches`` -- one row per preferred-method change.

    Workspace-owned (``WorkspaceScopedBase``) -- registered in
    ``app_shared.repository.WORKSPACE_OWNED_MODELS``. RLS'd via
    ``emit_rls_policy`` in the creating migration.
    """

    __tablename__ = "strategy_method_switches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_strategy_method_switches_workspace_id_workspaces",
        ),
        # The hot read is "the latest un-rolled-back switch for this
        # (profile, method_type)" -- issued once per flush cycle per
        # promoted/candidate method, so it must be an index scan.
        Index(
            "ix_sms_profile_method_switched_at",
            "domain_strategy_profile_id",
            "method_type",
            "switched_at",
        ),
    )

    domain_strategy_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    method_type: Mapped[str] = mapped_column(Text(), nullable=False)
    from_method: Mapped[str | None] = mapped_column(Text(), nullable=True)
    to_method: Mapped[str] = mapped_column(Text(), nullable=False)
    switched_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    #: Distinct qualifying URLs that justified the switch.
    evidence_samples: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    evidence_window_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: ``from_method``'s ``success_rate`` at switch time -- the bar the
    #: switch promised to beat.
    baseline_success_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    #: ``to_method``'s ``strategy_attempt_stats`` counter readings at
    #: switch time. Subtracting them from the current values isolates
    #: post-switch outcomes from a lifetime total.
    switch_attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )
    switch_success_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)


__all__ = ["StrategyMethodSwitch"]
