"""normalise api_key status

`api_keys.status` (`app_shared.enums.ApiKeyStatus`, via `enum_column` --
a plain `VARCHAR(32)`, never a Postgres-native enum, so the DB itself
never rejected an out-of-set value) has exactly two valid members:
`'active'` and `'revoked'`. `ApiKeyStatus` is a `StrEnum`, so any
historical row written before app-layer validation existed, written by
a since-removed code path, or hand-edited in the DB could carry
anything -- a stale value, different casing, `NULL`-ish garbage, etc.

`24cabfa` (Task 1, phase4-connect) made the runtime auth path fail
closed on such a row: `ApiKeyStatus(row.status)` now raises rather than
silently treating an unrecognised status as usable. This migration
closes the data-side half of the same gap: normalise every row whose
`status` is outside `('active', 'revoked')` to `'revoked'` -- fail
closed, never fail open. A key with a status we don't recognise MUST
NOT be treated as active by omission; the only safe unknown-status
default is "can't authenticate."

Revision ID: 5b9a86717a66
Revises: f87cf9a237cd
Create Date: 2026-08-11 16:32:30.084078

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5b9a86717a66'
down_revision: Union[str, Sequence[str], None] = 'f87cf9a237cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Any `api_keys.status` outside the valid set -> `'revoked'` (fail closed)."""
    op.execute(
        "UPDATE api_keys SET status = 'revoked' "
        "WHERE status NOT IN ('active', 'revoked')"
    )


def downgrade() -> None:
    """No-op: the original out-of-set values are not recoverable.

    This migration only ever moves a row from an unrecognised status to
    `'revoked'` and does not record what the prior value was, so there
    is nothing to restore. `'revoked'` is also the strictly safer state
    to leave a row in either way -- downgrading to "may as well have
    been unrecognised again" would just reopen the fail-open gap Task 1
    closed at the application layer.
    """
    pass
