"""partition network_operations by month (EPA C9)

Revision ID: a5e0c74b13d9
Revises: d1f7a3c9e284
Create Date: 2026-09-08 18:30:00.000000

EPA C9 (F14), plan §9.1, part 2 of 2. Turns ``network_operations`` — the
physical network ledger, the highest-cardinality table in the system —
into a monthly ``RANGE (created_at)`` partitioned table by the standard
build-copy-swap, so its retention can be a ``DROP TABLE`` of a whole
month instead of the bulk ``DELETE`` on a raw append-heavy table that
FR-015/SC-003 forbid.

THIS MIGRATION IS OWNER-RUN IN PRODUCTION
-----------------------------------------
It rewrites every row of the ledger. On a fleet-sized table that is a
long transaction holding an ``ACCESS EXCLUSIVE`` lock for the final swap,
and it roughly doubles the table's disk footprint until the legacy copy
is dropped. The EPA run that authored it executed it only against
throwaway databases. Run it in production behind a maintenance window,
with the D1 rehearsal's measured duration in hand.

WHAT IT DOES, IN ORDER
----------------------
1. **Idempotence gate.** If ``network_operations`` is already
   ``relkind = 'p'`` the whole migration is a no-op — re-running after a
   partial deploy is safe.
2. **Build.** ``network_operations_p`` is created ``LIKE
   network_operations INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING
   COMMENTS`` so it inherits every column, default, NOT NULL and CHECK
   that exists at run time — the shape is read from the live catalog,
   not re-typed here, so a column added by some later migration cannot be
   silently dropped by this one.
3. **Partitions.** One child per calendar month spanning
   ``[min(created_at), max(created_at)]`` plus the current and next
   month, named ``network_operations_YYYY_MM`` — the same convention
   ``app_shared.maintenance.partitions.partition_name`` constructs, so
   the running ``MAINTENANCE_PARTITION_CREATE`` job takes over seamlessly.
4. **Copy, in keyset batches.** A ``(created_at, id)`` keyset walk in
   50k-row batches. Not one ``INSERT ... SELECT``: a single statement
   over a fleet-sized ledger builds one enormous WAL record and gives no
   progress signal at all. The batches are inside one transaction (they
   have to be — the swap must be atomic with the copy), so this bounds
   memory and per-statement cost, not lock duration.
5. **Parity check.** Row counts must match exactly, or the migration
   raises and the whole transaction rolls back. A silent short copy is
   the one failure mode that would be discovered months later.
6. **Swap, in this transaction.** Constraint/index names are moved off
   the legacy table onto ``*_prepart``, the tables are renamed, and the
   new table's temporary ``*_p`` names are renamed to the canonical ones.
   The ``BEFORE UPDATE`` immutability trigger is recreated on the new
   parent (a row trigger on a partitioned table propagates to every
   child, current and future).
7. **RLS guard.** ``PARTITION_RLS_INHERITANCE_SQL`` is applied so every
   new child gets its parent's policies. ``network_operations`` is
   fleet-owned and carries no policy, so for THIS table the guard is a
   no-op — it is issued anyway because the guard is schema-wide and
   running it here keeps "a partition is created, the guard runs"
   invariant true at every seam that creates one.

THE LEGACY TABLE IS NOT DROPPED
-------------------------------
The old table survives as ``network_operations_pre_partition``. Dropping
it is an owner step (``DROP TABLE network_operations_pre_partition``),
deliberately outside this migration: it is the only rollback that does
not require re-copying, and disk is the cheaper of the two risks. The
``downgrade()`` below restores it, and refuses to run if it is gone.

FOREIGN KEYS DROPPED — THE MATERIAL CONSEQUENCE
-----------------------------------------------
A foreign key must reference a UNIQUE constraint, and a partitioned
table's unique constraint must include every partition-key column. So
``network_operations``' identity key becomes
``UNIQUE (network_request_id, created_at)`` and these five keys can no
longer exist:

* ``network_operations.retry_parent_id`` (self)
* ``network_operations.parent_operation_id`` (self)
* ``network_operation_allocations.operation_id``
* ``network_operation_settlements.operation_id``
* ``request_attempts.network_operation_id``

Re-establishing them would mean denormalising the parent's ``created_at``
onto four tables, one of which (``request_attempts``) is itself a
partitioned hot path. They are dropped instead, and the integrity they
enforced becomes a scheduled CHECK:
``app_shared.maintenance.ledger_summaries.find_orphan_references``.

Two things are genuinely weaker afterwards and are stated plainly rather
than buried:

* ``network_request_id`` is unique **per month**, not globally. It is a
  caller-minted UUIDv7, so a cross-month collision is not a thing that
  happens — but it is no longer a thing the database forbids.
* A referencing row can outlive the operation it names once that
  operation's partition is eventually dropped at 730 days. That is not
  new (retention would have had to break the FK anyway to drop a
  partition); it is now visible instead of impossible.

This trade is the reason the swap is an OWNER-run step: it should be
ratified, not merely deployed.
"""
from typing import Sequence, Union

from alembic import op

from app_shared.models.network_operations import (
    NETWORK_OPERATION_IMMUTABILITY_SQL,
)
from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL
from app_shared.models.workspace_usage_view import (
    RECREATE_WORKSPACE_USAGE_VIEW_SQL,
)


# revision identifiers, used by Alembic.
revision: str = 'a5e0c74b13d9'
down_revision: Union[str, Sequence[str], None] = 'd1f7a3c9e284'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: How many rows one copy statement moves. 50k keeps a single statement's
#: WAL record and sort footprint bounded on a fleet-sized ledger while
#: staying far above the per-statement planning overhead.
COPY_BATCH_ROWS = 50_000

#: Everything data-dependent lives in PL/pgSQL rather than in Python.
#: A ``DO`` block is ONE statement, so it renders correctly under
#: ``alembic upgrade --sql`` (offline mode, which has no connection to
#: query) and executes server-side online — the same trick the repo's
#: other data-shaped migrations use, and the only way this migration can
#: be both offline-renderable and driven by the table's real contents.
BUILD_AND_COPY_SQL = f"""
DO $$
DECLARE
    v_min       timestamptz;
    v_max       timestamptz;
    v_cursor    timestamptz;
    v_part_end  timestamptz;
    v_last_ts   timestamptz;
    v_last_id   uuid;
    v_prev_ts   timestamptz := '-infinity'::timestamptz;
    v_prev_id   uuid        := '00000000-0000-0000-0000-000000000000'::uuid;
    v_src_count bigint;
    v_dst_count bigint;
    v_child     text;
BEGIN
    IF (SELECT relkind FROM pg_class
         WHERE oid = 'public.network_operations'::regclass) = 'p' THEN
        RAISE NOTICE 'network_operations is already partitioned; nothing to do';
        RETURN;
    END IF;

    -- 2. Build. Shape comes from the live catalog, never re-typed here.
    CREATE TABLE network_operations_p (
        LIKE network_operations
        INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING COMMENTS
    ) PARTITION BY RANGE (created_at);

    ALTER TABLE network_operations_p
        ADD CONSTRAINT pk_network_operations_p PRIMARY KEY (id, created_at);
    ALTER TABLE network_operations_p
        ADD CONSTRAINT uq_network_operations_nrid_created_at_p
        UNIQUE (network_request_id, created_at);

    CREATE INDEX ix_network_operations_network_request_id_p
        ON network_operations_p (network_request_id);
    CREATE INDEX ix_network_operations_provider_created_at_p
        ON network_operations_p (provider, created_at);
    CREATE INDEX ix_network_operations_scrape_job_id_p
        ON network_operations_p (scrape_job_id);
    CREATE INDEX ix_network_operations_cuh_created_at_p
        ON network_operations_p (canonical_url_hash, created_at);

    -- 3. Partitions: every month the data actually occupies, plus the
    --    current and next month so writes land the moment the swap
    --    commits and the maintenance job has something to extend.
    SELECT min(created_at), max(created_at) INTO v_min, v_max
      FROM network_operations;
    v_min := date_trunc('month', COALESCE(v_min, now()));
    v_max := date_trunc('month', GREATEST(COALESCE(v_max, now()), now()))
             + interval '1 month';

    v_cursor := v_min;
    WHILE v_cursor <= v_max LOOP
        v_part_end := v_cursor + interval '1 month';
        v_child := format('network_operations_%s', to_char(v_cursor, 'YYYY_MM'));
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF network_operations_p '
            'FOR VALUES FROM (%L) TO (%L)',
            v_child, v_cursor, v_part_end);
        v_cursor := v_part_end;
    END LOOP;

    -- 4. Copy, in keyset batches over (created_at, id).
    LOOP
        SELECT created_at, id INTO v_last_ts, v_last_id
          FROM (
              SELECT created_at, id
                FROM network_operations
               WHERE (created_at, id) > (v_prev_ts, v_prev_id)
               ORDER BY created_at, id
               LIMIT {COPY_BATCH_ROWS}
          ) AS batch
         ORDER BY created_at DESC, id DESC
         LIMIT 1;

        EXIT WHEN v_last_ts IS NULL;

        INSERT INTO network_operations_p
        SELECT * FROM network_operations
         WHERE (created_at, id) >  (v_prev_ts, v_prev_id)
           AND (created_at, id) <= (v_last_ts, v_last_id);

        v_prev_ts := v_last_ts;
        v_prev_id := v_last_id;
        v_last_ts := NULL;
    END LOOP;

    -- 5. Parity. A short copy must abort the whole transaction.
    SELECT count(*) INTO v_src_count FROM network_operations;
    SELECT count(*) INTO v_dst_count FROM network_operations_p;
    IF v_src_count <> v_dst_count THEN
        RAISE EXCEPTION
            'network_operations partition copy is short: % source rows, % copied',
            v_src_count, v_dst_count;
    END IF;
    RAISE NOTICE 'network_operations: % rows copied into % partitions',
        v_dst_count,
        (SELECT count(*) FROM pg_inherits
          WHERE inhparent = 'public.network_operations_p'::regclass);
END
$$;
"""

#: The swap. Also one ``DO`` block, and deliberately the same
#: transaction as the copy: a crash between the two would leave a
#: complete but unreferenced shadow table.
SWAP_SQL = """
DO $$
DECLARE
    con    record;
    defmap jsonb := '{}'::jsonb;
    newname text;
BEGIN
    IF (SELECT relkind FROM pg_class
         WHERE oid = 'public.network_operations'::regclass) = 'p' THEN
        RAISE NOTICE 'network_operations is already partitioned; swap skipped';
        RETURN;
    END IF;

    -- The five foreign keys that cannot survive a partition key in the
    -- referenced unique constraint. See this migration's docstring.
    ALTER TABLE network_operation_allocations
        DROP CONSTRAINT IF EXISTS fk_noa_operation_id_network_operations;
    ALTER TABLE network_operation_settlements
        DROP CONSTRAINT IF EXISTS fk_nos_operation_id_network_operations;
    ALTER TABLE request_attempts
        DROP CONSTRAINT IF EXISTS
            fk_request_attempts_network_operation_id_network_operations;
    ALTER TABLE network_operations
        DROP CONSTRAINT IF EXISTS fk_no_retry_parent_id_network_operations;
    ALTER TABLE network_operations
        DROP CONSTRAINT IF EXISTS fk_no_parent_operation_id_network_operations;

    -- The trigger is recreated on the new parent below; dropping it here
    -- keeps the legacy table inert while it survives as a backup.
    DROP TRIGGER IF EXISTS trg_network_operations_immutable ON network_operations;

    -- Remember each CHECK constraint's canonical name by its definition,
    -- so the copies `LIKE ... INCLUDING CONSTRAINTS` made under
    -- system-generated names can be renamed back to it.
    FOR con IN
        SELECT conname, pg_get_constraintdef(oid) AS condef
          FROM pg_constraint
         WHERE conrelid = 'public.network_operations'::regclass
           AND contype = 'c'
    LOOP
        defmap := defmap || jsonb_build_object(con.condef, con.conname);
        EXECUTE format('ALTER TABLE network_operations RENAME CONSTRAINT %I TO %I',
                       con.conname, left(con.conname || '_prepart', 63));
    END LOOP;

    -- Move the legacy table's remaining well-known names out of the way.
    ALTER TABLE network_operations
        RENAME CONSTRAINT pk_network_operations TO pk_network_operations_prepart;
    ALTER TABLE network_operations
        RENAME CONSTRAINT uq_network_operations_network_request_id
                       TO uq_network_operations_nrid_prepart;
    ALTER INDEX ix_network_operations_provider_created_at
        RENAME TO ix_network_operations_provider_created_at_prepart;
    ALTER INDEX ix_network_operations_scrape_job_id
        RENAME TO ix_network_operations_scrape_job_id_prepart;
    ALTER INDEX ix_network_operations_canonical_url_hash_created_at
        RENAME TO ix_network_operations_cuh_created_at_prepart;

    ALTER TABLE network_operations RENAME TO network_operations_pre_partition;
    ALTER TABLE network_operations_p RENAME TO network_operations;

    -- Canonical names onto the new table.
    ALTER TABLE network_operations
        RENAME CONSTRAINT pk_network_operations_p TO pk_network_operations;
    ALTER TABLE network_operations
        RENAME CONSTRAINT uq_network_operations_nrid_created_at_p
                       TO uq_network_operations_network_request_id_created_at;
    ALTER INDEX ix_network_operations_network_request_id_p
        RENAME TO ix_network_operations_network_request_id;
    ALTER INDEX ix_network_operations_provider_created_at_p
        RENAME TO ix_network_operations_provider_created_at;
    ALTER INDEX ix_network_operations_scrape_job_id_p
        RENAME TO ix_network_operations_scrape_job_id;
    ALTER INDEX ix_network_operations_cuh_created_at_p
        RENAME TO ix_network_operations_canonical_url_hash_created_at;

    FOR con IN
        SELECT conname, pg_get_constraintdef(oid) AS condef
          FROM pg_constraint
         WHERE conrelid = 'public.network_operations'::regclass
           AND contype = 'c'
    LOOP
        newname := defmap ->> con.condef;
        IF newname IS NOT NULL AND newname <> con.conname THEN
            EXECUTE format(
                'ALTER TABLE network_operations RENAME CONSTRAINT %I TO %I',
                con.conname, newname);
        END IF;
    END LOOP;
END
$$;
"""

#: Undo. Only possible while the legacy table is still there — which is
#: exactly why this migration does not drop it.
DOWNGRADE_SQL = """
DO $$
BEGIN
    IF to_regclass('public.network_operations_pre_partition') IS NULL THEN
        RAISE EXCEPTION
            'cannot downgrade: network_operations_pre_partition is gone. '
            'The legacy copy was dropped, so the only way back is a restore.';
    END IF;

    DROP TRIGGER IF EXISTS trg_network_operations_immutable ON network_operations;
    DROP TABLE network_operations;
    ALTER TABLE network_operations_pre_partition RENAME TO network_operations;

    ALTER TABLE network_operations
        RENAME CONSTRAINT pk_network_operations_prepart TO pk_network_operations;
    ALTER TABLE network_operations
        RENAME CONSTRAINT uq_network_operations_nrid_prepart
                       TO uq_network_operations_network_request_id;
    ALTER INDEX ix_network_operations_provider_created_at_prepart
        RENAME TO ix_network_operations_provider_created_at;
    ALTER INDEX ix_network_operations_scrape_job_id_prepart
        RENAME TO ix_network_operations_scrape_job_id;
    ALTER INDEX ix_network_operations_cuh_created_at_prepart
        RENAME TO ix_network_operations_canonical_url_hash_created_at;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(BUILD_AND_COPY_SQL)
    op.execute(SWAP_SQL)
    # Review R13 (2026-09-09). `SWAP_SQL` renames the relation
    # `workspace_usage_v` was created over, and Postgres resolves a view's
    # dependencies to relation OIDs rather than names -- so at this point
    # the tenant usage view is still reading
    # `network_operations_pre_partition`: a table that receives no further
    # rows, and that can now never be dropped, because the view depends on
    # it. Both failures are silent. Recreating the view (its definition
    # names the table, so the new one rebinds to whatever currently holds
    # the name) is what makes the swap complete, and the grant must be
    # re-applied because a recreated view is a NEW object that carries
    # none of the old one's privileges. `CREATE OR REPLACE` would not do:
    # it cannot change a view's underlying relation binding.
    for statement in RECREATE_WORKSPACE_USAGE_VIEW_SQL:
        op.execute(statement)
    op.execute(NETWORK_OPERATION_IMMUTABILITY_SQL)
    op.execute(PARTITION_RLS_INHERITANCE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(DOWNGRADE_SQL)
    # The mirror of the recreation above. `DOWNGRADE_SQL` drops the
    # partitioned table and renames the legacy one back, which leaves the
    # view bound to a relation that no longer exists -- and, before that,
    # would have made the drop itself fail on the dependency. Same three
    # statements, same order, on the way back.
    for statement in RECREATE_WORKSPACE_USAGE_VIEW_SQL:
        op.execute(statement)
    op.execute(NETWORK_OPERATION_IMMUTABILITY_SQL)
