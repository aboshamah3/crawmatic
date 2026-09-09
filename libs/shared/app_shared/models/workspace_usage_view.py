"""The `workspace_usage_v` tenant view, as ONE definition (review R13).

Why this module exists
----------------------
`workspace_usage_v` is created by migration `193ac27f0dc2` over
`network_operations`. Migration `a5e0c74b13d9` later partitions that
table by build-copy-swap: the original relation is renamed to
`network_operations_pre_partition` and the new partitioned table takes
the old name. Postgres resolves a view's dependencies to relation OIDs at
creation time, not to names, so after the swap the view silently keeps
reading the LEGACY table — which stops receiving rows the moment the swap
commits, and which can then never be removed, because the view depends on
it. A stale tenant usage read and an unreclaimable copy of the largest
table in the system, neither of which raises anything.

The fix is to recreate the view after the rename. The reason it lives
HERE rather than being re-typed inside the swap migration is that a
re-typed copy is the same defect with a longer fuse: the next column
added to the view would be added in one place, and the swap would quietly
recreate the older shape. `app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL`
is the precedent — migrations already import schema-level SQL from this
package rather than duplicating it.

A recreated view is a NEW object
--------------------------------
Every grant on the old view disappears with it, so
:data:`GRANT_WORKSPACE_USAGE_VIEW_SQL` must be issued after every
recreation, not only the first one. It is guarded by a `pg_roles` lookup
for the same reason the original migration guarded it: a from-scratch CI
database may not have run `provision_db_roles.sql` yet, and a migration
that fails on a missing role is a migration that cannot bootstrap.

Only `crawmatic_app` is granted, deliberately: `crawmatic_auth` and
`crawmatic_scraper` have no business reading cross-tenant usage. See
`193ac27f0dc2`'s own module docstring for that decision.
"""

from __future__ import annotations

__all__ = [
    "CREATE_WORKSPACE_USAGE_VIEW_SQL",
    "DROP_WORKSPACE_USAGE_VIEW_SQL",
    "GRANT_WORKSPACE_USAGE_VIEW_SQL",
    "RECREATE_WORKSPACE_USAGE_VIEW_SQL",
    "WORKSPACE_USAGE_VIEW",
]

#: The view's name, in one place so a test can assert against it rather
#: than against a literal it re-types.
WORKSPACE_USAGE_VIEW = "workspace_usage_v"

#: The tenant fence. `current_setting(..., true)` returns NULL rather
#: than raising when the GUC is unset, and `NULLIF(..., '')` turns the
#: empty string into NULL too — so a session that never set a workspace
#: sees no rows instead of an error, and never sees another tenant's.
_WORKSPACE_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

#: The view definition. `network_operations` is named, never OID-pinned:
#: that is exactly what makes recreating this enough to rebind it onto
#: whichever relation currently holds the name.
CREATE_WORKSPACE_USAGE_VIEW_SQL = f"""
CREATE VIEW {WORKSPACE_USAGE_VIEW} AS
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

#: `IF EXISTS` so both directions are re-runnable after a partial deploy.
DROP_WORKSPACE_USAGE_VIEW_SQL = f"DROP VIEW IF EXISTS {WORKSPACE_USAGE_VIEW};"

#: Re-applied after EVERY creation — see the module docstring.
GRANT_WORKSPACE_USAGE_VIEW_SQL = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_app') THEN "
    f"GRANT SELECT ON {WORKSPACE_USAGE_VIEW} TO crawmatic_app; "
    "END IF; "
    "END $$;"
)

#: The three statements a rename must be followed by, in order. Kept as a
#: tuple rather than one blob because `op.execute` takes one statement at
#: a time and the offline `--sql` render is read by a DBA statement by
#: statement.
RECREATE_WORKSPACE_USAGE_VIEW_SQL: tuple[str, ...] = (
    DROP_WORKSPACE_USAGE_VIEW_SQL,
    CREATE_WORKSPACE_USAGE_VIEW_SQL,
    GRANT_WORKSPACE_USAGE_VIEW_SQL,
)
