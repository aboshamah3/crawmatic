"""rollup watermark and strategy switch audit

Revision ID: 05dfda7cdbb7
Revises: 9f24d748ba13
Create Date: 2026-08-26 02:23:06.122054

EPA W5.5-L2. Two tables:

* ``rollup_watermarks`` -- the durable cursor that makes the daily
  rollup crash-safe and catch-up-able (Item A). GLOBAL, no RLS --
  the same shape and rationale as ``maintenance_cadences``: a daily
  rollup is one cross-tenant window for the whole deployment.
* ``strategy_method_switches`` -- the durable, auditable record of
  every preferred-method change the optimizer makes, and of every
  rollback (Item B). WORKSPACE-OWNED -- RLS'd, because a row names
  one workspace's domain strategy profile.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app_shared.models.rls import emit_rls_policy


# revision identifiers, used by Alembic.
revision: str = '05dfda7cdbb7'
down_revision: Union[str, Sequence[str], None] = '9f24d748ba13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Item A: rollup_watermarks (GLOBAL, no RLS) ---------------
    op.create_table(
        "rollup_watermarks",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("last_complete_date", sa.Date(), nullable=False),
        sa.Column(
            "last_advanced_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "advance_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key", name="pk_rollup_watermarks"),
    )

    # --- Item B: strategy_method_switches (WORKSPACE-OWNED, RLS) --
    op.create_table(
        "strategy_method_switches",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "domain_strategy_profile_id", sa.Uuid(as_uuid=True), nullable=False
        ),
        sa.Column("method_type", sa.Text(), nullable=False),
        sa.Column("from_method", sa.Text(), nullable=True),
        sa.Column("to_method", sa.Text(), nullable=False),
        sa.Column("switched_at", sa.DateTime(timezone=True), nullable=False),
        # Distinct qualifying URLs that justified the switch.
        sa.Column(
            "evidence_samples", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "evidence_window_seconds", sa.Integer(), nullable=True
        ),
        # from_method's success_rate at switch time -- the bar the
        # switch promised to beat.
        sa.Column(
            "baseline_success_rate", sa.Numeric(5, 4), nullable=True
        ),
        # to_method's strategy_attempt_stats counter readings at switch
        # time. Subtracting them from the current values is what
        # isolates POST-switch outcomes from a lifetime total, which is
        # the only honest way to ask "did this switch make things
        # worse" from an aggregate-only stats table.
        sa.Column(
            "switch_attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "switch_success_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_method_switches"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_strategy_method_switches_workspace_id_workspaces",
        ),
    )
    op.create_index(
        "ix_strategy_method_switches_workspace_id",
        "strategy_method_switches",
        ["workspace_id"],
    )
    # The hot read is "the latest un-rolled-back switch for this
    # (profile, method_type)" -- issued once per flush cycle per
    # promoted/candidate method, so it must be an index scan.
    op.create_index(
        "ix_sms_profile_method_switched_at",
        "strategy_method_switches",
        ["domain_strategy_profile_id", "method_type", "switched_at"],
    )

    for statement in emit_rls_policy("strategy_method_switches"):
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_sms_profile_method_switched_at",
        table_name="strategy_method_switches",
    )
    op.drop_index(
        "ix_strategy_method_switches_workspace_id",
        table_name="strategy_method_switches",
    )
    op.drop_table("strategy_method_switches")
    op.drop_table("rollup_watermarks")
