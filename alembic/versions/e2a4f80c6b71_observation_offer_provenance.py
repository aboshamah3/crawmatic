"""observation offer/provenance columns (EPA C5)

Revision ID: e2a4f80c6b71
Revises: c1e7b93a40df
Create Date: 2026-09-08 12:05:00.000000

EPA C5 (F19), plan §11: five columns on ``price_observations`` that make
a persisted price ATTRIBUTABLE after the fact.

W3.1 gave this table the whole ``offer_*`` superset and a validated
pydantic contract to fill it, and nothing on the live scrape path ever
wrote a single one of those columns. C5 wires that path; these five are
what let a reader of a row answer, months later, *who read this price
and how well* -- which is the question a disputed repricing decision
actually turns on.

``extractor_version TEXT``
    Which extraction contract read it
    (``scrape_core.items.EXTRACTOR_VERSION``). Text because it names a
    contract, and contracts get names, not numbers. Deliberately not the
    package version: a dependency bump that changes no extraction
    behaviour must not invalidate the attribution of every historical
    row.

``profile_version INT``
    Which revision of the scrape profile configured that read. Distinct
    from the existing ``offer_profile_version`` (TEXT, part of the W3.1
    pydantic projection, free-form): this is the integer
    ``scrape_profiles.version`` counter, comparable and orderable, which
    is what "was this read under the profile revision we later reverted?"
    needs.

``confidence NUMERIC(5,4)``
    The extraction's own confidence in [0, 1]. The same fraction
    ``extraction_confidence`` carries, given its own column because it is
    the value the C5 ``match_current_prices`` conflict guard compares,
    and a column a guard depends on should not be one whose meaning is
    "whatever the SPEC-07 extractor happened to record".

``provenance TEXT``
    Which POLICY chose the reading that was persisted --
    ``first_hit``/``ranked_v1``/``none``
    (``scrape_core.items.PROVENANCE_*``). This is the column that makes
    the C11 flip auditable: after ``EXTRACTION_RANKING_POLICY`` moves to
    ``v1``, it is the only thing that distinguishes a price the chain
    chose from one the ranker chose.

``availability_state TEXT``
    ``available`` | ``unavailable`` | ``blocked`` | ``stale`` |
    ``conditional``. Availability as a first-class fact, separate from
    ``stock_status`` (which only ever knew IN_STOCK / OUT_OF_STOCK /
    UNKNOWN): "we were blocked", "the offer has expired" and "the price
    is conditional on a coupon" are three different reasons a price is
    not simply usable, and collapsing them into UNKNOWN is what makes an
    unavailable product indistinguishable from a failed scrape.

NO ``CHECK`` CONSTRAINT ON ``availability_state``, ON PURPOSE.
``price_observations`` is PARTITIONED, and ``ALTER TABLE ... ADD
CONSTRAINT ... CHECK`` on a partitioned table recurses into and
validates every partition inside the migration's transaction -- on a
table with months of monthly partitions that is a long lock on the
hottest write path in the system, in exchange for a second enforcement
point behind a single writer. The vocabulary is enforced at that writer
(``scrape_core.pipelines.AVAILABILITY_STATES``), which is where a bad
value can be rejected before it exists. This is the same posture
``offer_seller_type`` on this same table already documents.

SHAPE. All five are ``ADD COLUMN`` with NO default, which PostgreSQL
records in the catalog without rewriting the table or its partitions.
All five are NULLABLE: every pre-C5 row legitimately has none of them,
and ``NULL`` meaning "this row predates the provenance contract" is a
more useful statement than a back-filled guess would be. No backfill is
performed for exactly that reason.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2a4f80c6b71'
down_revision: Union[str, Sequence[str], None] = 'c1e7b93a40df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add the provenance columns."""
    op.add_column(
        "price_observations", sa.Column("extractor_version", sa.Text(), nullable=True)
    )
    op.add_column(
        "price_observations", sa.Column("profile_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "price_observations",
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
    )
    op.add_column(
        "price_observations", sa.Column("provenance", sa.Text(), nullable=True)
    )
    op.add_column(
        "price_observations", sa.Column("availability_state", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema: drop the provenance columns."""
    op.drop_column("price_observations", "availability_state")
    op.drop_column("price_observations", "provenance")
    op.drop_column("price_observations", "confidence")
    op.drop_column("price_observations", "profile_version")
    op.drop_column("price_observations", "extractor_version")
