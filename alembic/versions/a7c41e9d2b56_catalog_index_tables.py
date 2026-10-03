"""catalog index tables

Revision ID: a7c41e9d2b56
Revises: f1c7e2a9b3d4
Create Date: 2026-10-02 12:00:00.000000

The catalog index (plan 2026-10-02-catalog-index-core-engine): a
fleet-wide copy of PUBLIC storefront listings (about 4M products from
about 30k KSA stores), bulk-loaded by ``scripts/load_catalog_index.py``
and read by ``app_shared.catalog_index.lookup`` to propose competitor
matches.

* ``catalog_index_products``: one listing per (generation, source_rowid).
* ``catalog_index_codes``: lookup keys (kind ``g`` GTIN-14, ``m`` model
  code) per listing.
* ``catalog_index_loads``: one row per load; exactly one is ``active``.

GLOBAL, no RLS, filed SYSTEM in ``scripts/rls_table_manifest.txt``: the
rows describe public pages, carry no ``workspace_id`` and nothing that
names or narrows a workspace (the same test ``extraction_shadow_events``
passes). Runtime roles get SELECT only; the loader writes as the owner.

No foreign key between the two big tables, on purpose: a load writes a
whole new generation beside the old one and the loader deletes the old
generation afterwards, each table on its own.

ADDITIVE ONLY: three new tables, nothing existing is touched, so the
previous release runs unchanged against this schema. Hand-authored to
match ``app_shared.models.catalog_index`` exactly.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c41e9d2b56"
down_revision: Union[str, Sequence[str], None] = "f1c7e2a9b3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the three catalog index tables (global, no RLS)."""
    op.create_table(
        "catalog_index_products",
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_rowid", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("mpn", sa.Text(), nullable=True),
        sa.Column("gtin", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("store_verdict", sa.String(32), nullable=False),
        sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation", "source_rowid", name="pk_catalog_index_products"),
    )
    op.create_index(
        "ix_catalog_index_products_generation_domain",
        "catalog_index_products",
        ["generation", "domain"],
    )
    op.execute(
        "CREATE INDEX ix_catalog_index_products_title_tsv ON catalog_index_products "
        "USING gin (to_tsvector('simple', coalesce(title, '')))"
    )
    op.create_table(
        "catalog_index_codes",
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(1), nullable=False),
        sa.Column("code_key", sa.Text(), nullable=False),
        sa.Column("source_rowid", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "generation", "kind", "code_key", "source_rowid", name="pk_catalog_index_codes"
        ),
    )
    op.create_table(
        "catalog_index_loads",
        sa.Column("generation", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("products", sa.BigInteger(), nullable=True),
        sa.Column("codes", sa.BigInteger(), nullable=True),
        sa.Column("source_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.PrimaryKeyConstraint("generation", name="pk_catalog_index_loads"),
    )


def downgrade() -> None:
    """Drop the three catalog index tables."""
    op.drop_table("catalog_index_loads")
    op.drop_table("catalog_index_codes")
    op.execute("DROP INDEX IF EXISTS ix_catalog_index_products_title_tsv")
    op.drop_index("ix_catalog_index_products_generation_domain", table_name="catalog_index_products")
    op.drop_table("catalog_index_products")
