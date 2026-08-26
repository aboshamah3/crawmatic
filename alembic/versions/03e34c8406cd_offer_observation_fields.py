"""offer_observation_fields

Revision ID: 03e34c8406cd
Revises: c4b19e7a2f08
Create Date: 2026-08-26 00:35:42.777684

EPA W3.1 (2026-08-26, READY-012): persists the ``OfferObservation``
superset (`app_shared.observations.offer_observation`) alongside the
existing SPEC-07 ``price_observations`` columns, not replacing them.

Adds 33 nullable ``offer_*`` columns to ``price_observations`` — see
``app_shared.models.observations.PriceObservation``'s W3.1 comment block
for the full rationale (money via the existing ``Money`` contract,
free-text categoricals mirroring the ``network_operations`` precedent,
JSONB for compound/repeatable sub-structures). Every column is
NULLABLE: an existing row and an observation missing a fact are both
legitimately absent here (§7: unknown = NULL, never 0/"").

``price_observations`` is monthly-RANGE-partitioned by ``scraped_at``.
Unlike RLS policies (f2a6c1d80b37's finding — policies do NOT propagate
to partitions), a plain ``ALTER TABLE ... ADD COLUMN`` on a partitioned
PARENT **does** propagate to every existing and future partition (the
c4b19e7a2f08/d5e8a3c164f2 precedent for
``request_attempts.network_operation_id``) — so this migration needs no
per-partition loop.

No new relation is created (only columns on an existing, already-RLS'd
table), so ``scripts/rls_table_manifest.txt`` needs no new entry.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '03e34c8406cd'
down_revision: Union[str, Sequence[str], None] = 'c4b19e7a2f08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (name, SQLAlchemy type) pairs, in the exact order they are added and
#: dropped (reversed) — kept as one list so upgrade/downgrade can never
#: drift apart on which columns exist.
_OFFER_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("offer_source_url", sa.Text()),
    ("offer_canonical_url", sa.Text()),
    ("offer_domain", sa.Text()),
    ("offer_market", sa.Text()),
    ("offer_source_timezone", sa.Text()),
    ("offer_expected_identity", postgresql.JSONB()),
    ("offer_observed_identity", postgresql.JSONB()),
    ("offer_seller_name", sa.Text()),
    ("offer_seller_type", sa.Text()),
    ("offer_fulfillment", sa.Text()),
    ("offer_condition", sa.Text()),
    ("offer_item_price", sa.Numeric(precision=18, scale=4)),
    ("offer_list_price", sa.Numeric(precision=18, scale=4)),
    ("offer_shipping_cost", sa.Numeric(precision=18, scale=4)),
    ("offer_tax_included", sa.Boolean()),
    ("offer_fees", sa.Numeric(precision=18, scale=4)),
    ("offer_deposit", sa.Numeric(precision=18, scale=4)),
    ("offer_unit_price", sa.Numeric(precision=18, scale=4)),
    ("offer_landed_total", sa.Numeric(precision=18, scale=4)),
    ("offer_promotion_facts", postgresql.JSONB()),
    ("offer_access_method", sa.Text()),
    ("offer_profile_version", sa.Text()),
    ("offer_parser_version", sa.Text()),
    ("offer_raw_evidence_hash", sa.Text()),
    ("offer_strategy", sa.Text()),
    ("offer_discovery_confidence", sa.Numeric(precision=5, scale=4)),
    ("offer_identity_confidence", sa.Numeric(precision=5, scale=4)),
    ("offer_comparability_confidence", sa.Numeric(precision=5, scale=4)),
    ("offer_validation_reasons", postgresql.JSONB()),
    ("offer_expires_at", sa.DateTime(timezone=True)),
    ("offer_comparability_class", sa.Text()),
    ("offer_rejection_reason", sa.Text()),
    ("offer_human_review_required", sa.Boolean()),
]


def upgrade() -> None:
    """Add the 33 OfferObservation `offer_*` columns to `price_observations`
    (propagates to every existing + future partition automatically)."""
    for name, col_type in _OFFER_COLUMNS:
        op.add_column("price_observations", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    """Drop the `offer_*` columns, in reverse of the order they were added."""
    for name, _col_type in reversed(_OFFER_COLUMNS):
        op.drop_column("price_observations", name)
