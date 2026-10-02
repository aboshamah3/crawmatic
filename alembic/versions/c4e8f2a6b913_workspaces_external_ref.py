"""workspaces.external_ref unique (provisioning idempotency key)

Revision ID: c4e8f2a6b913
Revises: b3d9e5a17c42
Create Date: 2026-10-02 19:00:00.000000

Security E5: additive ``workspaces.external_ref TEXT NULL UNIQUE``. Existing
rows stay NULL (Postgres UNIQUE allows many NULLs); the owner backfills from
the SaaS side with ``scripts/security/backfill_workspace_external_ref.py``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8f2a6b913"
down_revision: Union[str, Sequence[str], None] = "b3d9e5a17c42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("external_ref", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_workspaces_external_ref", "workspaces", ["external_ref"])


def downgrade() -> None:
    op.drop_constraint("uq_workspaces_external_ref", "workspaces", type_="unique")
    op.drop_column("workspaces", "external_ref")
