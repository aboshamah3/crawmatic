"""job cancellation fence + target cancellation audit columns

Revision ID: c1d7a4e9b350
Revises: b8f3d61c9e02
Create Date: 2026-08-25 00:00:00.000000

EPA A2 (2026-08-25): the schema behind the only sanctioned way to close a
stranded scrape job — :func:`app_shared.jobs.cancellation.
cancel_and_reconcile_job`.

Three things land here:

1. ``scrape_jobs.cancellation_generation`` (int, NOT NULL, default 0) —
   **the fence**. A cancellation transaction bumps this and sets the job
   status to ``CANCELLED`` *before* anything outside the database is
   touched. Dispatch paths (B1/B2) and the result-persistence path read
   it as "the generation this work was authorized under"; a stale
   generation means the work was authorized before a cancellation and is
   rejected. This is what makes "the DB says CANCELLED" impossible to
   contradict after the fact, without pretending that the DB write, the
   Redis guard deletion, the Scrapyd cancel and the reservation release
   could ever be one transaction.

   ``server_default='0'`` (not merely a Python-side default) so existing
   rows and any writer that does not know about the column both land on
   generation 0, which is exactly "never cancelled".

2. ``scrape_job_targets.cancelled_reason`` / ``cancelled_by`` /
   ``cancelled_at`` — the durable audit trail of *why* and *by whom* a
   target was terminalized without a result. Cancellation must never
   invent a success, so a cancelled target is distinguishable from a
   completed one not only by status but by a recorded human decision.
   Nullable and unbackfilled: a NULL means "this row was never
   cancelled", which is correct for every pre-existing row.

3. **No DDL for the enum widening.** ``ScrapeTargetStatus`` gains a
   ``CANCELLED`` member in ``app_shared.enums`` in the same change, but
   every status column in this repo is a plain, app-validated
   ``VARCHAR(32)`` (``app_shared.enums.enum_column`` ->
   ``_AppValidatedEnumString``), never a Postgres-native ``ENUM`` and
   never a ``CHECK`` constraint — see the creating migration
   ``a6b0234cd4ad`` (``sa.Column("status", sa.String(length=32), ...)``).
   There is therefore nothing to ``ALTER TYPE``: the new member is a
   code-level vocabulary change only. Recorded here explicitly so a
   future reader does not go looking for a missing enum migration.

Purely additive and fully reversible — ``downgrade`` drops all four
columns. Dropping them loses cancellation history and resets every
fence to "never cancelled", so a downgrade must only ever be run on a
deployment that has also reverted the code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d7a4e9b350'
down_revision: Union[str, Sequence[str], None] = 'b8f3d61c9e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add the cancellation fence + target audit columns."""
    op.add_column(
        "scrape_jobs",
        sa.Column(
            "cancellation_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "scrape_job_targets",
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "scrape_job_targets",
        sa.Column("cancelled_by", sa.Text(), nullable=True),
    )
    op.add_column(
        "scrape_job_targets",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop the fence + target cancellation audit columns."""
    op.drop_column("scrape_job_targets", "cancelled_at")
    op.drop_column("scrape_job_targets", "cancelled_by")
    op.drop_column("scrape_job_targets", "cancelled_reason")
    op.drop_column("scrape_jobs", "cancellation_generation")
