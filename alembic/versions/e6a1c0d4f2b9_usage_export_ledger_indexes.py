"""Usage-export indexes: ledger parent lookups and bare time ranges (E6, 2026-09-29)

Revision ID: e6a1c0d4f2b9
Revises: d8c14b7f6a92
Create Date: 2026-09-29 12:00:00.000000

Why
---
`/v1/admin/usage` took 28.9 s for one busy hour in production (213,954
`network_operations`, 106,492 `request_attempts`), and the SaaS importer
gives up at 5 s -- usage import has been frozen since 2026-09-24 20:12.
The query now reaches the ledger through two equi-joins bounded on its
partition key (`app.services.admin_usage.build_usage_query`); these three
indexes are what those joins and the window scans need:

* `ix_network_operations_parent_operation_id` -- `(parent_operation_id)
  WHERE parent_operation_id IS NOT NULL`. A browser page's subresources are
  found by `parent_operation_id = <the attempt's operation>`; there was no
  index on it at all (`models/network_operations.py`), so every attempt
  scanned the ledger. Partial: most operations have no parent, and a NULL
  is never looked up.
* `ix_request_attempts_created_at` -- `(created_at)`. The export's window
  predicate is a bare `created_at` range with NO workspace predicate (it is
  fleet-wide by design); the only time index was
  `(workspace_id, created_at)`, which a range on the second column cannot
  use.
* `ix_price_observations_scraped_at` -- `(scraped_at)`, the same reasoning
  for the observation half of the export.

How (live, partitioned PostgreSQL 18)
------------------------------------
All three parents are RANGE-partitioned, and Postgres forbids
`CREATE INDEX CONCURRENTLY` on a partitioned parent. So each uses the
recipe `b3e7c9a15d42_hot_path_indexes.py` established (documented in
`f87cf9a237cd`), inside `autocommit_block()`:

1. `CREATE INDEX IF NOT EXISTS ... ON ONLY <parent>` -- an initially
   invalid parent index; locks no partition.
2. `CREATE INDEX CONCURRENTLY IF NOT EXISTS` the same definition on every
   existing partition (discovered from `pg_inherits` at run time, never
   hard-coded) -- no ACCESS EXCLUSIVE, no write outage.
3. `ALTER INDEX <parent index> ATTACH PARTITION <child index>` per child
   (guarded by a catalog probe: ATTACH has no IF NOT EXISTS); once every
   child is attached the parent index flips to valid.

Partitions created later by `app_shared.maintenance.partitions` get a
matching child index automatically from the parent definition.

Idempotent. Caveat, as in b3e7c9a15d42: an interrupted CONCURRENTLY build
leaves an INVALID child index that `IF NOT EXISTS` then skips -- drop it by
name (`pg_index.indisvalid = false`) and re-run.

No data is changed; RLS is unaffected (indexes are not row access).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6a1c0d4f2b9"
down_revision: Union[str, Sequence[str], None] = "d8c14b7f6a92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (parent index, parent table, column list + optional predicate, child suffix)
_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "ix_network_operations_parent_operation_id",
        "network_operations",
        "(parent_operation_id) WHERE parent_operation_id IS NOT NULL",
        "_parent_operation_id_idx",
    ),
    (
        "ix_request_attempts_created_at",
        "request_attempts",
        "(created_at)",
        "_created_at_idx",
    ),
    (
        "ix_price_observations_scraped_at",
        "price_observations",
        "(scraped_at)",
        "_scraped_at_idx",
    ),
)

_OFFLINE_NOTE = (
    "-- OFFLINE MODE: the per-partition CREATE INDEX CONCURRENTLY + ATTACH "
    "PARTITION steps for {index} are omitted -- they need a live catalog to "
    "enumerate {parent}'s partitions. Run against a live connection."
)


def _partitions_of(parent: str) -> list[str]:
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
            WHERE parent.relname = :parent
              AND parent.relnamespace = 'public'::regnamespace
            ORDER BY child.relname
            """
        ),
        {"parent": parent},
    ).fetchall()
    return [row[0] for row in rows]


def _attached(child_index: str, parent_index: str) -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT 1
                FROM pg_inherits
                JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
                JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
                WHERE child.relname = :child AND parent.relname = :parent
                """
            ),
            {"child": child_index, "parent": parent_index},
        )
        .fetchone()
        is not None
    )


def _child_index_name(child: str, suffix: str) -> str:
    # Postgres truncates identifiers at 63 bytes; keep the name deterministic
    # and inside the limit so the attach probe finds what CREATE made.
    return f"{child}{suffix}"[:63]


def upgrade() -> None:
    offline = op.get_context().as_sql
    with op.get_context().autocommit_block():
        for index, parent, definition, suffix in _INDEXES:
            op.execute(f"CREATE INDEX IF NOT EXISTS {index} ON ONLY {parent} {definition}")
            if offline:
                op.execute(_OFFLINE_NOTE.format(index=index, parent=parent))
                continue
            for child in _partitions_of(parent):
                child_index = _child_index_name(child, suffix)
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {child_index} "
                    f"ON {child} {definition}"
                )
                if not _attached(child_index, index):
                    op.execute(f"ALTER INDEX {index} ATTACH PARTITION {child_index}")


def downgrade() -> None:
    offline = op.get_context().as_sql
    with op.get_context().autocommit_block():
        for index, parent, _definition, suffix in reversed(_INDEXES):
            # Dropping the partitioned parent index drops every attached
            # child with it (CONCURRENTLY is not supported on a partitioned
            # index, so this one takes a brief lock).
            op.execute(f"DROP INDEX IF EXISTS {index}")
            if offline:
                continue
            for child in _partitions_of(parent):
                op.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS {_child_index_name(child, suffix)}"
                )
