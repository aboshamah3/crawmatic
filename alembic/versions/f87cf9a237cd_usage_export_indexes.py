"""Covering indexes for the SaaS usage export (PLAN §7.2, risk P2).

Both tables are RANGE-partitioned (`request_attempts` on `created_at`,
`price_observations` on `scraped_at`), so an index created on the parent
propagates to every existing and future partition — this is why the
export can bound its window on the partition key and still get an
index-backed scan per pruned partition.

`(workspace_id, <time>)` and not `(<time>, workspace_id)`: the export
groups by workspace after pruning to the window's partitions, so
workspace is the selective leading column within a partition.

**Deployment note (review finding I6f).** `CREATE INDEX` (the plain,
non-`CONCURRENTLY` form used below) on a partitioned PARENT table takes
`ACCESS EXCLUSIVE` on the parent AND every existing partition for the
duration of the build on each one -- in practice a write outage on
`request_attempts` (and separately, `price_observations`) for however
long each partition's index build takes. Worse, PostgreSQL forbids
`CREATE INDEX CONCURRENTLY` directly on a partitioned parent -- there is
no drop-in non-blocking equivalent of this migration as written. Do NOT
run this migration as-is against a production table with live traffic.

The zero-downtime recipe, if this ever needs to be re-run online:

1. `CREATE INDEX ... ON ONLY <parent>` -- registers the index on the
   parent WITHOUT touching any partition or requiring a partition-wide
   lock (the parent-only index starts out marked invalid).
2. For each existing partition, `CREATE INDEX CONCURRENTLY` the
   equivalent index directly on that partition (no `ACCESS EXCLUSIVE`,
   no write outage).
3. `ALTER INDEX <parent index> ATTACH PARTITION <partition index>` for
   each partition -- once every partition's index is attached, the
   parent index is automatically marked valid.

Repeat per partition per table (`request_attempts`, `price_observations`).
New partitions created after this runs still need their own `CREATE
INDEX CONCURRENTLY` + `ATTACH PARTITION` (or a job that does it for
every newly created partition) since the parent's "ON ONLY" registration
does not retroactively cover partitions that did not exist at the time.

Revision ID: f87cf9a237cd
Revises: 03dec3037c8f
Create Date: 2026-08-11 00:52:44.085495

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f87cf9a237cd"
down_revision: Union[str, Sequence[str], None] = "03dec3037c8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_request_attempts_workspace_id_created_at",
        "request_attempts",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_price_observations_workspace_id_scraped_at",
        "price_observations",
        ["workspace_id", "scraped_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_price_observations_workspace_id_scraped_at",
        table_name="price_observations",
    )
    op.drop_index(
        "ix_request_attempts_workspace_id_created_at", table_name="request_attempts"
    )
