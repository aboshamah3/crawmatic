"""rls_on_partitions

Revision ID: f2a6c1d80b37
Revises: c7e1a9d34b60
Create Date: 2026-08-20 00:00:00.000000

Security review finding **A1** (2026-08-20), reproduced live against a
real Postgres before this migration was written.

THE DEFECT
----------
Four tables are RANGE-partitioned by month — ``request_attempts``,
``price_observations``, ``price_alert_events``, ``webhook_events`` —
and every one of their children carries ``workspace_id``. RLS was
enabled on the PARENTS only, because
``app_shared.maintenance.partitions.create_missing_partitions`` recorded
the belief that "RLS on the partitioned parent already propagates to
every child, current and future".

That is true of a query that names the parent, and false of a query that
names a child. PostgreSQL applies the policies of the relation the query
actually references; a partition created by ``CREATE TABLE ... PARTITION
OF`` has none of its own, and ``scripts/sql/rls_roles.sql`` §4 grants
``crawmatic_app`` SELECT on ``ALL TABLES IN SCHEMA public`` — partitions
included. So, as ``crawmatic_app`` with workspace A's context set:

    SELECT * FROM request_attempts             -> 1 row  (correct)
    SELECT * FROM request_attempts_2026_08     -> 2 rows (workspace B's too)

Eight partitions existed in that state. This migration closes them.

WHY THE MIGRATION IS ONLY ONE THIRD OF THE FIX
----------------------------------------------
A migration fixes the partitions that exist when it runs. Two other
paths keep making new ones, and each is closed in this same change:

* ``create_missing_partitions`` creates next month's partition at
  runtime, every month, forever — it now applies the same statement
  immediately after any ``CREATE TABLE ... PARTITION OF``, so a
  partition is born isolated rather than repaired a deploy later;
* ``migrate.provision_roles`` applies it on every deploy AND now
  **verifies** the invariant from the ``workspace_id`` column
  (``workspace_scoped_rls_problems``) instead of from ``relrowsecurity``
  — read the old way, a table with no RLS at all was not even a
  candidate for the check, which is exactly how eight partitions stayed
  invisible to it.

The statement itself is
``app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL``: it mirrors each
parent's ENABLE/FORCE switches and each of its policies onto its
children, reading ``pg_policy`` rather than restating any policy, so a
dual-scope parent's read/write pair is copied as faithfully as a strict
single policy. Importing the shared renderer is this repo's established
migration convention (``emit_rls_policy`` is imported the same way by
SPEC-04..16's migrations).

Idempotent, and safe to re-run: a child that already carries a policy of
the parent's name is skipped.

DOWNGRADE
---------
Deliberately a no-op in the sense that matters: it drops the mirrored
policies and the RLS switches from the partitions, restoring the
*previous* — leaky — state, because that is what a downgrade means. It
is never the right answer to a production incident; roll forward.
"""

from __future__ import annotations

from alembic import op

from app_shared.models.rls import PARTITION_RLS_INHERITANCE_SQL

# revision identifiers, used by Alembic.
revision = "f2a6c1d80b37"
down_revision = "c7e1a9d34b60"
branch_labels = None
depends_on = None


#: Mirror of the upgrade: strip every policy from every partition in
#: ``public`` and disable RLS on it. Written the same ``%``-free,
#: ``:``-free way as the upgrade statement so it is legal as a psql
#: body, an ``op.execute`` and a SQLAlchemy ``text()`` alike.
_UNDO_SQL = """DO $$
DECLARE
    part record;
    pol  record;
BEGIN
    FOR part IN
        SELECT child.oid AS child_oid,
               quote_ident(ns.nspname) || '.' || quote_ident(child.relname) AS child_ident
        FROM pg_class AS child
        JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
        WHERE ns.nspname = 'public'
          AND child.relkind = 'r'
          AND child.relispartition
          AND child.relrowsecurity
    LOOP
        FOR pol IN
            SELECT p.polname FROM pg_policy p WHERE p.polrelid = part.child_oid
        LOOP
            EXECUTE 'DROP POLICY ' || quote_ident(pol.polname) || ' ON ' || part.child_ident;
        END LOOP;
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' NO FORCE ROW LEVEL SECURITY';
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' DISABLE ROW LEVEL SECURITY';
    END LOOP;
END
$$;"""


def upgrade() -> None:
    op.execute(PARTITION_RLS_INHERITANCE_SQL)


def downgrade() -> None:
    op.execute(_UNDO_SQL)
