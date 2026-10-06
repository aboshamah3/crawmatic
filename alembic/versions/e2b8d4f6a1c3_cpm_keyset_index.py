"""competitor_product_matches (workspace_id, created_at, id) keyset index

Revision ID: e2b8d4f6a1c3
Revises: d7a1f3c5e902
Create Date: 2026-10-06 12:00:00.000000

Risk review 2026-10-06, P6. ``GET /v1/variants/competitor-prices``
(``apps/api/app/routers/variants.py::list_all_competitor_prices``) pages
the whole workspace's ``competitor_product_matches`` by
``workspace_id = :ws AND (created_at, id) > (:c, :i) ORDER BY created_at,
id LIMIT n``, and the SaaS read-model sync calls it every few minutes.
No index led with ``workspace_id`` and carried the keyset columns, so each
page sorted the workspace's matches. This one serves the walk directly,
with or without the route's default ``status <> 'ARCHIVED'`` filter (a
plain index rather than a partial one so ``include_archived=true`` is
served too).

Online-safe: ``CREATE INDEX CONCURRENTLY`` in an ``autocommit_block()``
(the ``b3e7c9a15d42_hot_path_indexes`` idiom). An interrupted build leaves
an INVALID index that ``IF NOT EXISTS`` would then skip: drop it by name
(``pg_index.indisvalid = false``) and re-run.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e2b8d4f6a1c3"
down_revision: Union[str, Sequence[str], None] = "d7a1f3c5e902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_cpm_ws_created_id"
_TABLE = "competitor_product_matches"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} (workspace_id, created_at, id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
