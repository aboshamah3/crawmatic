"""fleet daily cost/freshness scorecard (EPA D5)

Revision ID: f6b28c714a93
Revises: a5e0c74b13d9
Create Date: 2026-09-08 21:00:00.000000

EPA D5 (deep dive §12 item 9). One table: ``fleet_daily_scorecard``, one
row per UTC day, holding the whole-fleet cost/freshness figures
``app_shared.maintenance.scorecard`` computes and
``GET /admin/scorecard`` (``apps/api/app/routers/admin_ops.py``) reads.

SHAPE
-----
``date`` (DATE) is the PRIMARY KEY — the natural key IS the identity, the
same ``rollup_completion``/``rollup_watermarks`` precedent (no surrogate
``id`` column at all).

Every metric column is NULLABLE with no server default: a day this task
could not measure a given input for gets ``NULL`` on that column, never
``0`` — see the ORM model's docstring
(``app_shared.models.fleet_daily_scorecard``) for why a fabricated zero
would be worse than an honest "unmeasured".

GLOBAL, NO RLS
--------------
No ``workspace_id`` and no ``emit_rls_policy`` — one scorecard day
summarises the whole fleet, the same shape and rationale as
``rollup_completion``/``rollup_watermarks``/``maintenance_cadences``.
Classified SYSTEM in ``scripts/rls_table_manifest.txt``; explicit grants
for all three application roles are in ``scripts/sql/grants_expected.yaml``
and applied by ``scripts/provision_db_roles.sql`` — identical posture to
``rollup_completion`` (``crawmatic_app``: DELETE/INSERT/SELECT;
``crawmatic_auth``: DELETE/INSERT/SELECT/UPDATE; ``crawmatic_scraper``:
none).

NOT SEEDED
----------
No backfill of historical days. The first row this table ever holds is
whichever UTC day ``MAINTENANCE_DAILY_SCORECARD`` first runs for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6b28c714a93'
down_revision: Union[str, Sequence[str], None] = 'a5e0c74b13d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "fleet_daily_scorecard",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("provider_bytes", sa.BigInteger(), nullable=True),
        sa.Column("railway_cpu_seconds", sa.Float(), nullable=True),
        sa.Column("railway_ram_gb_hours", sa.Float(), nullable=True),
        sa.Column("railway_egress_gb", sa.Float(), nullable=True),
        sa.Column("valid_fresh_matches", sa.Integer(), nullable=True),
        sa.Column("attempts_per_valid_fresh", sa.Float(), nullable=True),
        sa.Column("browser_share", sa.Float(), nullable=True),
        sa.Column("proxied_share", sa.Float(), nullable=True),
        sa.Column("queue_oldest_seconds_p95", sa.Float(), nullable=True),
        sa.Column("persistence_lag_seconds_p95", sa.Float(), nullable=True),
        sa.Column("missing_metric_fraction", sa.Float(), nullable=True),
        sa.Column("budget_reserved_usd", sa.Float(), nullable=True),
        sa.Column("budget_settled_usd", sa.Float(), nullable=True),
        sa.Column("backup_egress_gb", sa.Float(), nullable=True),
        sa.Column("cost_per_valid_fresh_micro_usd", sa.Float(), nullable=True),
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
        sa.PrimaryKeyConstraint("date", name="pk_fleet_daily_scorecard"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("fleet_daily_scorecard")
