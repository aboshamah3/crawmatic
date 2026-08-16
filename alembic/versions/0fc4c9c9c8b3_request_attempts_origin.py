"""request_attempts_origin

Revision ID: 0fc4c9c9c8b3
Revises: b3e7c9a15d42
Create Date: 2026-08-16 00:00:00.000000

Task 2.3 (proxy-cost-reduction plan §2.3, safety prerequisite for §3.3):
adds ``request_attempts.origin`` (``'scrape'`` | ``'discovery'``,
NOT NULL, ``server_default='scrape'``) so the domain-strategy discovery
probe ladder (``apps/workers/app/workers/tasks_strategy.py::
_probe_sample``) can start writing its own audit rows without silently
reclassifying every pre-existing (and future ordinary) scrape-pipeline
row. `request_attempts` measured 87,082 real DataImpulse proxy requests
against 646 recorded rows for one domain (2026-08-15) — discovery probes
wrote **no** row at all, so per-URL accounting and the
`REQUESTS_PER_URL` circuit-breaker condition were blind to the largest
paid source.

Purely additive — ``server_default`` backfills every existing row to
``'scrape'`` in the same DDL statement (no separate data migration, no
``UPDATE`` pass). `request_attempts` is a monthly-RANGE-partitioned
table (``2db33dea5e14``); `ALTER TABLE ... ADD COLUMN` on the
partitioned **parent** propagates to every existing partition
automatically (the same "applied once, not per-partition" rule
``2db33dea5e14``'s docstring documents for RLS) — no per-partition
``op.execute`` is needed here, and none is emitted.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0fc4c9c9c8b3'
down_revision: Union[str, Sequence[str], None] = 'b3e7c9a15d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add request_attempts.origin (NOT NULL, default 'scrape')."""
    op.add_column(
        "request_attempts",
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default="scrape",
        ),
    )


def downgrade() -> None:
    """Downgrade schema: drop request_attempts.origin."""
    op.drop_column("request_attempts", "origin")
