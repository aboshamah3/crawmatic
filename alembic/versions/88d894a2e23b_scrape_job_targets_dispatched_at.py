"""scrape_job_targets dispatched_at

Revision ID: 88d894a2e23b
Revises: f2a6c1d80b37
Create Date: 2026-08-22 14:37:58.360178

F-2 (crawl-integrity plan, 2026-08-22): dispatch is currently
re-POSTing the whole backlog to scrapyd because there is no per-target
record of "we already sent this one." This adds
``scrape_job_targets.dispatched_at`` — the moment this target's batch
was last POSTed to scrapyd, NULL meaning never dispatched. Later tasks
in the same plan read it to make dispatch selection idempotent and use
it as the aging key for per-target stall detection.

Purely additive, nullable, no backfill needed (existing rows read as
"never dispatched", which is correct for anything dispatched before
this column existed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '88d894a2e23b'
down_revision: Union[str, Sequence[str], None] = 'f2a6c1d80b37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add scrape_job_targets.dispatched_at (nullable)."""
    op.add_column(
        "scrape_job_targets",
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop scrape_job_targets.dispatched_at."""
    op.drop_column("scrape_job_targets", "dispatched_at")
