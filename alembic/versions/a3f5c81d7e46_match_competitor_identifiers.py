"""match_competitor_identifiers + canonical_variant_ref

Revision ID: a3f5c81d7e46
Revises: e7b21f3a8c94
Create Date: 2026-08-25 00:00:00.000000

EPA B4 (2026-08-25, READY-003, fixes P0.7A): typed competitor identifiers.

``competitor_product_matches.competitor_variant_identifier`` is one
**untyped** text slot. The Shopify adapter compared it straight against
``variants[].id``; on S-Tech (``stech.ink``) it in fact holds a product
handle, a barcode, or a supplier SKU, so on 2026-08-24 the production
canary answered terminal ``NOT_LISTED`` for 26 of 30 targets whose
product JSON had come back HTTP 200 and perfectly healthy
(``PRODUCTION_READINESS_REPORT_2026-08-24.md``). A false delisting
silently removes a real competitor price from every comparison.

Two things land here.

1. ``match_competitor_identifiers`` — one row per (match, type, value),
   so a match can hold a handle **and** a SKU **and** a barcode **and** a
   variant id at once, each with its own provenance (``source``), trust
   (``verified_at``/``confidence``) and validity window
   (``effective_from``/``effective_to``). History is kept by *closing* a
   row, never by overwriting one.

   ``uq_mci_match_type_value_current`` is a partial unique index on
   ``(match_id, identifier_type, value)`` WHERE ``effective_to IS NULL``:
   at most one *current* row per typed value, enforced by the database,
   while any number of closed historical rows for the same pair remain
   welcome. That is what makes the backfill idempotent — a second
   ``--apply`` cannot double-insert.

2. ``competitor_product_matches.canonical_variant_ref`` — a nullable FK
   to the identifier row currently selected as *the* variant identity.
   Nullable on purpose: "we do not yet know which identifier is
   canonical" is a real state, and inventing one is the bug this
   migration exists to fix. ``ON DELETE SET NULL`` — closing or removing
   an identifier must never delete a match.

The two tables reference each other, so the FK on the parent is added by
a separate ``ALTER`` *after* both exist (mirrored in the ORM by
``use_alter=True``).

The legacy ``competitor_variant_identifier`` column is deliberately
**NOT dropped**. It becomes read-only: the rollback anchor and audit
trail for ``scripts/migrate_stech_identifiers.py``. Dropping it would
make the backfill irreversible, which is the opposite of the point.

RLS: ``match_competitor_identifiers`` carries **no** ``workspace_id`` of
its own (same shape as ``match_audit_classifications`` /
``strategy_attempt_stats``), so isolation is transitive
(``EXISTS``-via-parent) through its FK to ``competitor_product_matches``,
applied via ``emit_fk_transitive_rls_policy`` in this SAME migration
(§32, Principle II).

**No DDL for the new enums.** ``CompetitorIdentifierType`` /
``CompetitorIdentifierSource`` are app-validated ``VARCHAR(32)``
(``app_shared.enums.enum_column`` -> ``_AppValidatedEnumString``), never
a Postgres-native ``ENUM`` — the convention every status column in this
repo follows. Likewise the new ``ScrapeErrorCode.IDENTITY_UNRESOLVED``
and ``MatchClassificationState.NEEDS_REVIEW`` members need no
``ALTER TYPE`` and no widening migration (both columns are already
``VARCHAR(32)``; the longest new value is 19 characters).

Reversible: ``downgrade`` drops the FK + column and then the table. It
loses every typed identifier and every canonical selection, so a
downgrade must only run on a deployment that has also reverted the code
(and, if the backfill was applied, after replaying its rollback CSV).

Hand-authored (matches ``app_shared.models.competitor_identifiers`` and
``app_shared.models.competitors_matches`` exactly) — this build
environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app_shared.models import emit_fk_transitive_rls_policy

# revision identifiers, used by Alembic.
revision: str = 'a3f5c81d7e46'
down_revision: Union[str, Sequence[str], None] = 'e7b21f3a8c94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: typed identifier child table (+RLS) and the canonical ref."""
    op.create_table(
        "match_competitor_identifiers",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("match_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("identifier_type", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_match_competitor_identifiers"),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["competitor_product_matches.id"],
            name="fk_mci_match_id_competitor_product_matches",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_mci_match_id", "match_competitor_identifiers", ["match_id"])
    op.create_index(
        "ix_mci_match_id_current",
        "match_competitor_identifiers",
        ["match_id"],
        postgresql_where=sa.text("effective_to IS NULL"),
    )
    op.create_index(
        "ix_mci_type_value",
        "match_competitor_identifiers",
        ["identifier_type", "value"],
    )
    # At most one CURRENT row per (match, type, value) — the database is
    # what makes the backfill idempotent, not application discipline.
    op.create_index(
        "uq_mci_match_type_value_current",
        "match_competitor_identifiers",
        ["match_id", "identifier_type", "value"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL"),
    )

    # The selected identity. Added AFTER the child table exists — the two
    # tables reference each other.
    op.add_column(
        "competitor_product_matches",
        sa.Column("canonical_variant_ref", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_cpm_canonical_variant_ref_mci",
        "competitor_product_matches",
        "match_competitor_identifiers",
        ["canonical_variant_ref"],
        ["id"],
        ondelete="SET NULL",
    )

    # RLS in the SAME migration that creates the table (§32, Principle
    # II): transitive (EXISTS-via-parent) isolation, since this child
    # carries no workspace_id column of its own — the
    # match_audit_classifications (b8f3d61c9e02) precedent.
    for statement in emit_fk_transitive_rls_policy(
        "match_competitor_identifiers",
        parent_table="competitor_product_matches",
        fk_column="match_id",
    ):
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema: drop the canonical ref, then the identifier table."""
    op.drop_constraint(
        "fk_cpm_canonical_variant_ref_mci",
        "competitor_product_matches",
        type_="foreignkey",
    )
    op.drop_column("competitor_product_matches", "canonical_variant_ref")
    op.drop_index(
        "uq_mci_match_type_value_current", table_name="match_competitor_identifiers"
    )
    op.drop_index("ix_mci_type_value", table_name="match_competitor_identifiers")
    op.drop_index("ix_mci_match_id_current", table_name="match_competitor_identifiers")
    op.drop_index("ix_mci_match_id", table_name="match_competitor_identifiers")
    # The table's RLS policy is dropped with the table itself.
    op.drop_table("match_competitor_identifiers")
