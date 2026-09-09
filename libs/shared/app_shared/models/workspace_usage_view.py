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

The fix is to recreate the view after the rename. It ships as its own
alembic revision (`4b06b233e0b8`, chained after the swap) rather than as
an edit to either existing one, because both `193ac27f0dc2` and
`a5e0c74b13d9` have already been applied to databases: an applied
revision is history, and editing it means two environments at the same
`alembic_version` no longer have the same schema. `193ac27f0dc2`
therefore keeps its own inline `CREATE VIEW` verbatim — frozen, not
duplicated-on-purpose — and everything from the rebind forward issues the
constants below.

The reason the recreation lives HERE rather than being re-typed inside
the rebind migration is that a re-typed copy is the same defect with a
longer fuse: the next column added to the view would be added in one
place, and some later migration would quietly recreate the older shape.
`app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL` is the precedent —
migrations already import schema-level SQL from this package rather than
duplicating it.

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
    "LEGACY_NETWORK_OPERATIONS_TABLE",
    "NETWORK_OPERATIONS_TABLE",
    "REBIND_WORKSPACE_USAGE_VIEW_TO_LEGACY_SQL",
    "RECREATE_WORKSPACE_USAGE_VIEW_SQL",
    "WORKSPACE_USAGE_VIEW",
    "create_workspace_usage_view_sql",
]

#: The view's name, in one place so a test can assert against it rather
#: than against a literal it re-types.
WORKSPACE_USAGE_VIEW = "workspace_usage_v"

#: The tenant fence. `current_setting(..., true)` returns NULL rather
#: than raising when the GUC is unset, and `NULLIF(..., '')` turns the
#: empty string into NULL too — so a session that never set a workspace
#: sees no rows instead of an error, and never sees another tenant's.
_WORKSPACE_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

#: The relation the view reads. After `a5e0c74b13d9` this name belongs
#: to the PARTITIONED table; before it, to the plain one. Either way the
#: view names it rather than pinning an OID, which is exactly what makes
#: recreating the view enough to rebind it.
NETWORK_OPERATIONS_TABLE = "network_operations"

#: What `a5e0c74b13d9` renamed the pre-swap table to. Only the downgrade
#: path names it: a downgrade has to leave the schema as the revision
#: below it left it, and the revision below it left the view bound here.
LEGACY_NETWORK_OPERATIONS_TABLE = "network_operations_pre_partition"


def create_workspace_usage_view_sql(source_table: str = NETWORK_OPERATIONS_TABLE) -> str:
    """The view definition, over `source_table`.

    One body, two callers: the rebind (over the live, partitioned
    `network_operations`) and its own downgrade (over the legacy copy, so
    that stepping back down lands on the schema the revision below this
    one actually produced). Column list and tenant fence are identical by
    construction -- a downgrade that quietly reshaped the view would be a
    second R13.
    """
    return f"""
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
FROM {source_table} no_
JOIN network_operation_allocations noa
    ON noa.operation_id = no_.network_request_id
WHERE noa.workspace_id = {_WORKSPACE_CTX};
"""


#: The view definition. `network_operations` is named, never OID-pinned:
#: that is exactly what makes recreating this enough to rebind it onto
#: whichever relation currently holds the name. Byte-identical to the
#: inline copy frozen inside `193ac27f0dc2` at the time of the rebind --
#: `tests/unit/test_migration_offline_workspace_usage_view.py` pins that
#: equality, so the rebind cannot silently reshape the view a merchant's
#: usage read already depends on.
CREATE_WORKSPACE_USAGE_VIEW_SQL = create_workspace_usage_view_sql()

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


#: The downgrade half, and the one place `network_operations_pre_partition`
#: is named. Guarded rather than unconditional: this revision is what makes
#: the legacy copy droppable, so by the time anyone downgrades it may
#: already be gone -- in which case `a5e0c74b13d9`'s own downgrade refuses
#: too ("the only way back is a restore"), and leaving the view bound to
#: the live table is strictly better than dropping it for a step back that
#: cannot complete. Written as one `DO` block because the guard has to be
#: evaluated by the server, not by the migration process: the offline
#: `--sql` render has no database to ask.
REBIND_WORKSPACE_USAGE_VIEW_TO_LEGACY_SQL = f"""
DO $$
BEGIN
    IF to_regclass('public.{LEGACY_NETWORK_OPERATIONS_TABLE}') IS NULL THEN
        RAISE NOTICE
            '{WORKSPACE_USAGE_VIEW} left bound to {NETWORK_OPERATIONS_TABLE}: '
            '{LEGACY_NETWORK_OPERATIONS_TABLE} is gone, so there is nothing to '
            'rebind to and the partition swap below cannot be undone either.';
        RETURN;
    END IF;

    DROP VIEW IF EXISTS {WORKSPACE_USAGE_VIEW};
    {create_workspace_usage_view_sql(LEGACY_NETWORK_OPERATIONS_TABLE).strip()}

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_app') THEN
        GRANT SELECT ON {WORKSPACE_USAGE_VIEW} TO crawmatic_app;
    END IF;
END
$$;
"""
