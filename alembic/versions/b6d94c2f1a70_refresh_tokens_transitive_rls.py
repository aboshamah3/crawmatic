"""refresh_tokens transitive RLS via users.user_id

Revision ID: b6d94c2f1a70
Revises: d5e8a3c164f2
Create Date: 2026-08-25 00:00:00.000000

EPA B8b (2026-08-25, READY-007 / P0.5): closes the one ``GAP`` the B8
audit left open in ``scripts/rls_table_manifest.txt``.

``refresh_tokens`` holds ``token_hash`` + ``user_id`` and carried **no**
row-level security on production or on a freshly migrated database.
``crawmatic_app`` holds ``SELECT`` on it through the blanket schema
grant, so any tenant connection could read every user's refresh-token
hashes across every workspace. The hash is not the bearer token, but it
is an authentication artefact and the row set alone leaks the platform's
entire user graph (how many users each workspace has, when they last
signed in, whether an account is still live).

The table carries no ``workspace_id`` of its own, so isolation is
**transitive** through its real FK to ``users`` — the
``match_audit_classifications`` (b8f3d61c9e02) /
``match_competitor_identifiers`` (a3f5c81d7e46) precedent, rendered by
the same :func:`emit_fk_transitive_rls_policy`:

    EXISTS (SELECT 1 FROM users p
             WHERE p.id = refresh_tokens.user_id
               AND p.workspace_id = NULLIF(
                     current_setting('app.workspace_id', true), '')::uuid)

Fail-closed by the same ``NULLIF(..., '')`` guard every other policy in
this schema uses: with no ``app.workspace_id`` set the inner predicate is
never true for any parent row, so the ``EXISTS`` is never true — zero
rows, never an error, never all rows (SC-005). A ``SUPER_ADMIN`` user has
``workspace_id IS NULL``, which is likewise never equal to a context, so
its tokens are invisible to **every** tenant connection.

**Why this does not break authentication.** Every statement the platform
issues against ``refresh_tokens`` runs BEFORE a workspace context could
exist — the rotation and the revocation are keyed by an unforgeable
``token_hash`` with no principal resolved yet, and the issue happens for
a user whose home workspace may be ``NULL``. All three therefore run on
the sanctioned BYPASSRLS auth seam (``app_shared.database.
get_auth_session`` / the ``crawmatic_auth`` role), exactly as the
pre-auth user-by-email lookup already did; ``apps/api/app/routers/
auth.py`` was moved onto it in the same packet as this migration. The
policy below is therefore a confinement of ``crawmatic_app`` — the role
that has no business reading this table at all — and not a filter any
auth path depends on.

Reversible: ``downgrade`` drops the policy and returns the table to
``NO FORCE`` / ``DISABLE ROW LEVEL SECURITY``, i.e. exactly the posture
head ``d5e8a3c164f2`` leaves it in. No data is touched in either
direction.

Hand-authored (RLS DDL is not autogenerable) — this build environment
has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app_shared.models import emit_fk_transitive_rls_policy

# revision identifiers, used by Alembic.
revision: str = 'b6d94c2f1a70'
down_revision: Union[str, Sequence[str], None] = 'd5e8a3c164f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Named here (not inlined) so upgrade and downgrade cannot drift: the
#: emitter's default is `{table}_workspace_isolation`.
POLICY_NAME = "refresh_tokens_workspace_isolation"
TABLE_NAME = "refresh_tokens"


def upgrade() -> None:
    """Enable + FORCE RLS on refresh_tokens, scoped transitively via users."""
    for statement in emit_fk_transitive_rls_policy(
        TABLE_NAME,
        parent_table="users",
        fk_column="user_id",
    ):
        op.execute(statement)


def downgrade() -> None:
    """Return refresh_tokens to the unprotected posture of d5e8a3c164f2."""
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {TABLE_NAME};")
    op.execute(f"ALTER TABLE {TABLE_NAME} NO FORCE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE_NAME} DISABLE ROW LEVEL SECURITY;")
