"""tenant usage view (A3/F03)

Revision ID: 193ac27f0dc2
Revises: a2f0217c9d43
Create Date: 2026-09-07

EPA A3 (F03, "Component-scoped credentials and grants"). Creates the
tenant-safe view ``workspace_usage_v`` over ``network_operations``,
filtered by the RLS workspace key via a join to
``network_operation_allocations`` (the only one of the three ledger
tables from EPA C1 that carries a ``workspace_id`` — ``network_operations``
itself is deliberately fleet-owned, no ``workspace_id`` column at all; see
``app_shared.models.network_operations``'s module docstring).

DELIBERATE FILENAME/CONTENT MISMATCH — role creation is NOT here
------------------------------------------------------------------
The packet this migration was written under names this file
``<rev>_tenant_usage_view_and_scraper_role.py`` and its acceptance
criteria describe ONE Alembic revision creating both the
``crawmatic_scraper`` role AND this view. That is not what this revision
does, and the omission is deliberate, not an oversight:

``scripts/provision_db_roles.sql``'s own header already documents "WHY
THIS IS NOT AN ALEMBIC MIGRATION" for the other three roles (crawmatic_
app/auth/migrate) — roles are CLUSTER-level objects, shared across every
database in the cluster, and must exist BEFORE the service that
authenticates as them. More concretely for THIS migration:
``crawmatic_migrate`` — the role Alembic authenticates as in production
via ``MIGRATION_DATABASE_URL`` (see provision_db_roles.sql section 3) —
is deliberately provisioned ``NOSUPERUSER`` and, implicitly, without
``CREATEROLE`` (it is granted exactly ``CREATE`` on schema ``public`` and
nothing else). A migration that runs ``CREATE ROLE crawmatic_scraper``
would fail with a permission error the moment it runs against a real
production deployment authenticated as ``crawmatic_migrate`` — it would
only appear to work in a test/compose environment where migrations
happen to run as a superuser or the database owner.

``crawmatic_scraper`` is therefore created by
``scripts/provision_db_roles.sql`` section 3a (mirroring sections 1-3 for
the other three roles) and its full component-scoped privilege set is
applied by that same file's section 5, sourced from
``scripts/sql/grants_expected.yaml``. This migration creates ONLY the
view — a schema object, which is exactly what Alembic (running as
``crawmatic_migrate``, which DOES own schema objects) is for.

The view
--------
``workspace_usage_v`` joins ``network_operations`` to
``network_operation_allocations`` on ``operation_id =
network_request_id`` and additionally filters by the SAME fail-closed
workspace GUC expression ``app_shared.models.rls.emit_rls_policy`` uses
elsewhere (``NULLIF(current_setting('app.workspace_id', true), '')::
uuid``). This is belt-and-suspenders, not decorative:
``network_operation_allocations`` already carries its own ENABLE+FORCE
RLS policy (added at head c4b19e7a2f08), so the join alone would already
restrict results to the caller's own allocations; restating the filter in
the view means the view's own WHERE clause draws the same fail-closed
line even if the view is ever queried by a role whose RLS posture on the
underlying tables changes. No RLS is (or can meaningfully be) enabled ON
the view itself — Postgres views are not directly RLS-able relations;
the security boundary here is the WHERE clause plus the underlying
table's FORCE ROW LEVEL SECURITY.

Granted only SELECT to ``crawmatic_app`` in this migration (the
ordinary tenant connection) — ``crawmatic_auth`` (BYPASSRLS) has no need
for a workspace-filtered view of a table it can already read directly
under its narrow system seam, and ``crawmatic_scraper`` has no evidenced
need for usage reporting at all (see ``scripts/sql/grants_expected.yaml``).
Re-running ``provision_db_roles.sql`` after this migration is idempotent
and does not touch this grant (the view is not named in its generated
grant loops, which only iterate ``scripts/rls_table_manifest.txt``'s base
tables); this migration's own ``GRANT`` is therefore the durable source
for this one privilege, consistent with how every other GRANT this repo
ships inside a migration (e.g. ``app_shared.models.rls.emit_rls_policy``'s
statements) is applied in the same migration that creates its object.

``downgrade()`` drops the view.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '193ac27f0dc2'
down_revision: Union[str, Sequence[str], None] = 'a2f0217c9d43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_WORKSPACE_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

_CREATE_VIEW_SQL = f"""
CREATE VIEW workspace_usage_v AS
SELECT
    noa.workspace_id,
    no_.network_request_id,
    no_.domain,
    no_.http_method,
    no_.provider,
    no_.transport,
    no_.created_at,
    no_.closed_at,
    no_.bytes_compressed,
    no_.bytes_decompressed,
    no_.response_status,
    no_.duration_ms,
    noa.fraction_ppb,
    noa.allocated_cost_micro_units,
    noa.currency
FROM network_operations no_
JOIN network_operation_allocations noa
    ON noa.operation_id = no_.network_request_id
WHERE noa.workspace_id = {_WORKSPACE_CTX};
"""

_DROP_VIEW_SQL = "DROP VIEW IF EXISTS workspace_usage_v;"


def upgrade() -> None:
    """Upgrade schema: create the tenant-safe workspace_usage_v view."""
    op.execute(_CREATE_VIEW_SQL)
    # crawmatic_app only -- see module docstring for why crawmatic_auth
    # and crawmatic_scraper are deliberately not granted this view.
    # Idempotent-safe to re-run in a fresh database that has not yet run
    # provision_db_roles.sql (the role may not exist yet in a from-
    # scratch CI database); guarded the same way section 7 of that file
    # guards its own catalog-driven repairs.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_app') THEN "
        "GRANT SELECT ON workspace_usage_v TO crawmatic_app; "
        "END IF; "
        "END $$;"
    )


def downgrade() -> None:
    """Downgrade schema: drop the workspace_usage_v view."""
    op.execute(_DROP_VIEW_SQL)
