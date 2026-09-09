"""domain rules request timeout

Revision ID: f4a19c7d2b80
Revises: e7b34c0af219
Create Date: 2026-09-08 00:00:00.000000

EPA C1 (F08): adds ``domain_rules.request_timeout_seconds`` — the
per-domain request timeout ``maintenance.domain_timeout_tune``
(``app_shared.maintenance.domain_timeouts``) learns from that domain's
own successful attempts: ``clamp(1.5 x p95(successful attempt duration,
7 d), 10 s, 60 s)``.

WHY. One global timeout (``SCRAPE_DOWNLOAD_TIMEOUT_SECONDS``, 60 s)
applies to every domain today, so the deep dive's "46.9 s average
proxied-HTTP attempt" is a measurement of that ceiling, not of any
domain: every fetch that was going to fail costs the full minute of a
worker slot, a fleet lease and real proxy egress first. A domain that
answers successfully in ~1 s does not need 60.

SHAPE. Exactly the ``ALTER TABLE ... ADD COLUMN`` with no default that
``e7b34c0af219`` said this would be: nullable, no server default —
catalog-only in PostgreSQL 11+, no table rewrite, no backfill, and every
existing row keeps behaving exactly as it did. ``NULL`` means "use the
setting" (never "unlimited", never "zero"), consistent with
``fleet_concurrency``/``fleet_rate_per_minute`` on the same table.

``domain_rules`` remains GLOBAL and RLS-free (no ``workspace_id``, filed
SYSTEM in ``scripts/rls_table_manifest.txt``): a domain's timeout is a
property of the domain and of the whole fleet, not of any workspace, so
this column adds no tenant surface.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4a19c7d2b80'
down_revision: Union[str, Sequence[str], None] = 'e7b34c0af219'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add ``domain_rules.request_timeout_seconds``."""
    op.add_column(
        "domain_rules",
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop ``domain_rules.request_timeout_seconds``."""
    op.drop_column("domain_rules", "request_timeout_seconds")
