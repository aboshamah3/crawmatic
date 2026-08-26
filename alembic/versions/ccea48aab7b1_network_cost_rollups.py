"""network_cost_rollups (EPA C6)

Revision ID: ccea48aab7b1
Revises: 7c2b9e5a41d6
Create Date: 2026-08-26 03:10:00.000000

Two tables backing the C6 cost-metrics publication surface (`GET
/ops/metrics`'s new fleet-health `cost_rollup` section, and the new
tenant-scoped `GET /v1/cost-rollups`): both are populated by
``app_shared.netledger.rollups.run_cost_rollup`` on a durable cursor
(``rollup_watermarks``, key ``"network_cost_rollup"`` — reusing W5.5-L2's
table with this run's own key namespace rather than a new watermark
store), never computed synchronously on request. See
``app_shared.models.network_cost_rollups`` for the full ownership
rationale, the bounded top-N + "other" cardinality contract, and what
"method"/"profile-version" mean here (``method`` is the operation's
TRANSPORT — ``DIRECT``/``PROXY``/``BROWSER`` — and ``profile_version``
is the domain playbook's own version; neither is the HTTP verb or the
per-decision budget tag, both of which are degenerate as grouping keys).

* ``fleet_network_cost_rollups`` — FLEET-scoped (no ``workspace_id``, no
  RLS), mirroring ``network_operations``' own shape. Filed SYSTEM (not
  GAP) in ``scripts/rls_table_manifest.txt``: a fleet SUM across every
  tenant is not tenant-linked evidence the way a raw ledger row is.
* ``network_cost_rollups`` — WORKSPACE-owned, RLS'd in THIS migration
  (the standard precedent: RLS lands with the table that needs it).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app_shared.models.rls import emit_rls_policy

# revision identifiers, used by Alembic.
revision: str = 'ccea48aab7b1'
down_revision: Union[str, Sequence[str], None] = '7c2b9e5a41d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- fleet_network_cost_rollups (FLEET, no RLS) --------------------
    op.create_table(
        "fleet_network_cost_rollups",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rollup_date", sa.Date(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column(
            "profile_version", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "operation_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "estimated_cost_minor_units",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reconciled_cost_minor_units", sa.BigInteger(), nullable=True
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_fleet_network_cost_rollups"),
        sa.UniqueConstraint(
            "rollup_date",
            "domain",
            "method",
            "profile_version",
            "currency",
            name="uq_fncr_date_domain_method_version_currency",
        ),
        sa.CheckConstraint(
            "operation_count >= 0", name="fncr_operation_count_non_negative"
        ),
        sa.CheckConstraint(
            "estimated_cost_minor_units >= 0",
            name="fncr_estimated_cost_non_negative",
        ),
        sa.CheckConstraint(
            "reconciled_cost_minor_units IS NULL OR reconciled_cost_minor_units >= 0",
            name="fncr_reconciled_cost_non_negative",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="fncr_currency_is_iso4217"),
    )
    op.create_index(
        "ix_fncr_rollup_date", "fleet_network_cost_rollups", ["rollup_date"]
    )

    # --- network_cost_rollups (WORKSPACE, RLS in this migration) ------
    op.create_table(
        "network_cost_rollups",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rollup_date", sa.Date(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column(
            "profile_version", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "operation_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "estimated_cost_minor_units",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reconciled_cost_minor_units", sa.BigInteger(), nullable=True
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_network_cost_rollups"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_ncr_workspace_id_workspaces",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "rollup_date",
            "domain",
            "method",
            "profile_version",
            name="uq_ncr_workspace_date_domain_method_version",
        ),
        sa.CheckConstraint(
            "operation_count >= 0", name="ncr_operation_count_non_negative"
        ),
        sa.CheckConstraint(
            "estimated_cost_minor_units >= 0",
            name="ncr_estimated_cost_non_negative",
        ),
        sa.CheckConstraint(
            "reconciled_cost_minor_units IS NULL OR reconciled_cost_minor_units >= 0",
            name="ncr_reconciled_cost_non_negative",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ncr_currency_is_iso4217"),
    )
    op.create_index(
        "ix_ncr_workspace_id_rollup_date",
        "network_cost_rollups",
        ["workspace_id", "rollup_date"],
    )

    for statement in emit_rls_policy("network_cost_rollups"):
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_ncr_workspace_id_rollup_date", table_name="network_cost_rollups")
    op.drop_table("network_cost_rollups")
    op.drop_index("ix_fncr_rollup_date", table_name="fleet_network_cost_rollups")
    op.drop_table("fleet_network_cost_rollups")
