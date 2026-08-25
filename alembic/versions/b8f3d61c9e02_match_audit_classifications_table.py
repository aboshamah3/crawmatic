"""match_audit_classifications_table

Revision ID: b8f3d61c9e02
Revises: 6e4a9c8f2d10
Create Date: 2026-08-25 00:00:00.000000

EPA A6 (2026-08-25): a versioned sidecar over ``competitor_product_matches``
holding the Mushtryati match-set audit classification (``ACTIVE`` /
``CONFIRMED_DELISTED`` / ``INVALID_IDENTITY`` / ``UNKNOWN``, see
``app_shared.enums.MatchClassificationState``). A sidecar, NOT a column
on ``competitor_product_matches`` — classifications are versioned facts
(supersede, never overwrite; ``superseded_at`` is the only column ever
touched on an existing row), so the matches table stays untouched.

**No** ``workspace_id`` column of its own (same shape as
``strategy_attempt_stats``, ``f30c60cfa2f7``): isolation is anchored
*transitively* through the real FK to ``competitor_product_matches``
(itself workspace-owned) via ``emit_fk_transitive_rls_policy``, applied
in this SAME migration (§32, Principle II).

``uq_mac_match_id_current`` — a partial unique index on ``match_id``
WHERE ``superseded_at IS NULL`` — enforces the single-current-row
invariant (at most one non-superseded classification per match) at the
database level, not just by application discipline.

Hand-authored (matches ``app_shared.models.match_audit`` exactly) —
this build environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app_shared.models import emit_fk_transitive_rls_policy

# revision identifiers, used by Alembic.
revision: str = 'b8f3d61c9e02'
down_revision: Union[str, Sequence[str], None] = '6e4a9c8f2d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create match_audit_classifications + transitive RLS."""
    op.create_table(
        "match_audit_classifications",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("match_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("classifier_version", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reviewer", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_match_audit_classifications"),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["competitor_product_matches.id"],
            name="fk_mac_match_id_competitor_product_matches",
        ),
    )
    op.create_index(
        "ix_mac_match_id", "match_audit_classifications", ["match_id"]
    )
    op.create_index(
        "uq_mac_match_id_current",
        "match_audit_classifications",
        ["match_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    # RLS in the SAME migration that creates the table (§32, Principle
    # II): transitive (EXISTS-via-parent) isolation, since this sidecar
    # carries no workspace_id column of its own (research D3 precedent,
    # f30c60cfa2f7's strategy_attempt_stats).
    for statement in emit_fk_transitive_rls_policy(
        "match_audit_classifications",
        parent_table="competitor_product_matches",
        fk_column="match_id",
    ):
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema: drop match_audit_classifications (RLS/policy drop with it)."""
    op.drop_table("match_audit_classifications")
