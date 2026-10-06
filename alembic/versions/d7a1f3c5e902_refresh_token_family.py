"""refresh_tokens.family_id (refresh-token reuse detection)

Revision ID: d7a1f3c5e902
Revises: c4e8f2a6b913
Create Date: 2026-10-02 21:00:00.000000

Security E10: additive ``refresh_tokens.family_id UUID NULL`` + index. One
login starts a family; every rotation inherits it; presenting an already
rotated token revokes every live token in its family. Existing rows stay
NULL and adopt a family the first time they are rotated
(``ROTATE_REFRESH_TOKEN_SQL``), so no backfill is needed. The table's
transitive RLS policy (b6d94c2f1a70) is keyed on ``user_id`` and is
unaffected.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a1f3c5e902"
down_revision: Union[str, Sequence[str], None] = "c4e8f2a6b913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("family_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
