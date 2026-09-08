"""extraction_shadow_events (EPA C5)

Revision ID: c1e7b93a40df
Revises: a7c31d0f9e42
Create Date: 2026-09-08 12:00:00.000000

EPA C5 (F19), plan §11: the durable evidence behind the C11 owner
decision on ``EXTRACTION_RANKING_POLICY``.

The W3.2 ranked extraction path has been correct and tested since
2026-08-26 and has never decided a live price, because turning it on is
a pricing-behaviour change and nobody had a number for how often it
would disagree with the first-hit chain it would replace. ``shadow``
mode produces that number: the chain still decides, the ranker also
runs, and every MATERIAL disagreement lands here.

Written by ``scrape_core.pipelines._flush_batch`` draining the bounded
in-process buffer ``scrape_core.extraction.pipeline`` fills -- inside
the same transaction as the observations it shadows, so a rolled-back
flush takes its measurements with it and the recorded rate is always
over prices that actually exist.

FLEET-SCOPED: NO ``workspace_id``, NO RLS. A classification, not an
omission. The row is evidence about an EXTRACTION POLICY applied to a
DOMAIN, and the decision it feeds is fleet-wide (one flag, one fleet).
The same competitor page is read on behalf of every workspace that
tracks it, so any single ``workspace_id`` here would be an arbitrary
pick among them and the sum over tenants is the only meaningful
aggregation anyway. Filed SYSTEM in ``scripts/rls_table_manifest.txt``
alongside ``domain_playbooks``; granted INSERT to the ingestion role
that writes it and SELECT to the system role that reports on it,
nothing to a tenant read path.

NO ``CHECK`` on ``disagreement_kind``/``ranked_outcome``. Both
vocabularies live in one producer
(``scrape_core.extraction.pipeline._shadow_disagreement``) and are
enforced there; a DB constraint on a fast-append telemetry table buys a
second enforcement point at the cost of a migration every time the
producer learns to distinguish a new kind of disagreement -- which is
exactly what this table exists to let it do.

Indexes: ``observed_at`` (every question about this table is
"disagreements in window W") and ``domain`` (the C11 gate is decided per
domain against the C4 labeled sets, not fleet-wide in aggregate).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1e7b93a40df'
down_revision: Union[str, Sequence[str], None] = 'a7c31d0f9e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create the fleet-scoped shadow-event table."""
    op.create_table(
        "extraction_shadow_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # Producer clock, not write clock: the two differ by a flush
        # interval, and a rate computed over write time smears a burst
        # into the wrong window.
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        # The ranking policy version the shadow run used. NOT NULL: a
        # gate decision is a statement about a SPECIFIC policy, and a
        # later revision must not silently inherit this one's evidence.
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        # `price` | `currency` | `outcome`. A different METHOD reaching
        # the same price and currency is not a disagreement and produces
        # no row at all.
        sa.Column("disagreement_kind", sa.Text(), nullable=False),
        sa.Column("first_hit_method", sa.Text(), nullable=True),
        # Money, not float: NUMERIC(18,4), the same contract
        # `price_observations.price` uses, because these two numbers are
        # compared against each other and against labeled truth.
        sa.Column("first_hit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("first_hit_currency", sa.Text(), nullable=True),
        # `winner` | `conflict` | `no_valid`.
        sa.Column("ranked_outcome", sa.Text(), nullable=False),
        sa.Column("ranked_method", sa.Text(), nullable=True),
        sa.Column("ranked_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("ranked_currency", sa.Text(), nullable=True),
        # Content address of the DECODED page text the extractor read --
        # deliberately not guaranteed equal to the observation's
        # `offer_raw_evidence_hash`, which names the raw bytes as they
        # arrived. For a page whose declared and actual encodings differ
        # those are two different byte sequences, and pretending
        # otherwise would produce an address for bytes nobody has.
        sa.Column("page_evidence_hash", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_shadow_events"),
    )
    op.create_index(
        "ix_extraction_shadow_events_observed_at",
        "extraction_shadow_events",
        ["observed_at"],
    )
    op.create_index(
        "ix_extraction_shadow_events_domain",
        "extraction_shadow_events",
        ["domain"],
    )


def downgrade() -> None:
    """Downgrade schema: drop the table and its indexes."""
    op.drop_index(
        "ix_extraction_shadow_events_domain", table_name="extraction_shadow_events"
    )
    op.drop_index(
        "ix_extraction_shadow_events_observed_at", table_name="extraction_shadow_events"
    )
    op.drop_table("extraction_shadow_events")
