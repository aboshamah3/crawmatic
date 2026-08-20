"""scrape_profiles_json_path_columns

Revision ID: c7e1a9d34b60
Revises: 0fc4c9c9c8b3
Create Date: 2026-08-16 00:00:00.000000

Task 3.1 (proxy-cost-reduction plan §3.1, handover 2026-08-15 §7): adds
the five nullable ``scrape_profiles.*_json_path`` columns the
``EMBEDDED_JSON`` extraction strategy
(``scrape_core.extraction.embedded_json``) resolves against embedded
JSON blobs on a page — ``<script id="__NEXT_DATA__">``,
``<script type="application/json">``, and ``var X = {...};`` assignment
bodies.

They mirror the existing ``*_selector`` / ``*_xpath`` / ``*_regex``
column families one-for-one (``price`` / ``old_price`` / ``currency`` /
``stock`` / ``title``) and carry the same opt-in contract: every column
is ``NULL`` by default, and a profile whose ``price_json_path`` is
``NULL`` makes the strategy an immediate no-op, so this migration cannot
change extraction behaviour for any existing profile. Purely additive —
five ``ALTER TABLE ... ADD COLUMN ... NULL`` statements, no backfill, no
data migration, no rewrite (Postgres adds a nullable column without a
default as a catalog-only change).

``scrape_profiles`` is a small, unpartitioned, non-RLS table, so there
is no partition-propagation or policy consideration here.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7e1a9d34b60'
down_revision: Union[str, Sequence[str], None] = '0fc4c9c9c8b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_PATH_COLUMNS: tuple[str, ...] = (
    "price_json_path",
    "old_price_json_path",
    "currency_json_path",
    "stock_json_path",
    "title_json_path",
)


def upgrade() -> None:
    """Upgrade schema: add the five nullable scrape_profiles.*_json_path columns."""
    for column in _JSON_PATH_COLUMNS:
        op.add_column("scrape_profiles", sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema: drop the five scrape_profiles.*_json_path columns."""
    for column in reversed(_JSON_PATH_COLUMNS):
        op.drop_column("scrape_profiles", column)
