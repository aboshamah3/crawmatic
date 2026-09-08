"""dispatch_intents.node_url + a deterministic, NOT NULL scrapyd_job_id

Revision ID: b6e5d1c94a72
Revises: a4e91c7d2b58
Create Date: 2026-09-07 00:00:00.000000

EPA B2 (2026-09-07, F06): the schema half of "commit the dispatch intent
BEFORE the POST, and give the remote run a name we chose".

What actually changes here, and what merely gets documented
-----------------------------------------------------------
B1 (``e7b21f3a8c94``) already created ``dispatch_intents`` with
``state``, ``posted_at`` and ``confirmed_at``, so those three columns of
F06's list exist and are untouched. ``state`` also needs no DDL for its
new ``RECONCILED_MISSING`` member: ``DispatchIntentState`` is an
app-validated ``VARCHAR(32)`` (``app_shared.enums.enum_column`` ->
``_AppValidatedEnumString``), never a Postgres-native ``ENUM``, which is
the convention every status column in this repo follows. There is
nothing to ``CREATE TYPE`` or ``ALTER TYPE``.

Two things are genuinely new.

1. ``node_url TEXT NOT NULL`` (server default ``''``). The intent has
   always recorded the node *class* — ``{project}:{spider}``, the pool —
   deliberately, so that a pool resize cannot mint a new identity for
   work already POSTed. But recovery needs the pool *member*: asking
   every node in a pool whether it has a job is strictly more work and
   strictly less certain than asking the one node the POST actually went
   to. ``NOT NULL`` with an empty-string default rather than a nullable
   column so "no node recorded" is exactly one value; the pre-B2 rows the
   backfill leaves at ``''`` are handled explicitly by
   ``ScrapydDispatchClient.list_jobs``, which refuses to guess for them.

2. ``scrapyd_job_id`` becomes ``uuid NOT NULL``. This is the F06 change
   with teeth, and it is a change of *meaning* as much as of type: the
   column used to hold whatever Scrapyd answered (nullable, because until
   the answer arrived there was nothing to hold). It now holds the id
   **we** chose at plan time —
   ``uuid5(SCRAPYD_JOB_ID_NAMESPACE, identity.key)``, see
   ``app_shared.jobs.dispatch_intents.deterministic_scrapyd_job_id`` —
   and POST it to Scrapyd's ``schedule.json`` ``jobid`` form field, which
   Scrapyd 1.6 honours verbatim (``.. versionchanged:: 1.2.0``; EPA B3
   Step 0 confirmed both deployed nodes are 1.6.0).

   That is what makes a re-POST safe. A ``POSTED`` intent the maintenance
   reconciler finds on no node moves to ``RECONCILED_MISSING`` and is
   re-POSTed **with the same id**, so a node that did in fact receive the
   original request dedups it instead of running the batch twice. A
   nullable, node-minted id could not support that: the id would not
   exist until after the very POST whose fate is in question.

Backfill, and why it rides the rewrite instead of an UPDATE
-----------------------------------------------------------
The backfill of existing rows is expressed as the ``USING`` expression
of the ``ALTER COLUMN ... TYPE uuid`` rewrite, **not** as a preceding
``UPDATE``. That is not a style choice — an ``UPDATE`` here is silently
wrong:

``dispatch_intents`` is a WORKSPACE table carrying ``FORCE ROW LEVEL
SECURITY`` plus this repo's fail-closed policy (``workspace_id =
NULLIF(current_setting('app.workspace_id', true), '')::uuid``, see
``app_shared.models.rls.emit_rls_policy``). Production migrations
authenticate as ``crawmatic_migrate``, which is ``NOBYPASSRLS`` *and*
owns the table, so ``FORCE`` applies to it; Alembic sets no
``app.workspace_id`` GUC, so the policy matches **zero rows**. An
``UPDATE dispatch_intents SET ...`` therefore reports ``UPDATE 0`` and
raises nothing — the migration marches on to ``SET NOT NULL`` and fails
there against any real data (measured on a restored production copy in
the EPA B10 release rehearsal: 2,040 rows in 1 workspace, aborting the
upgrade). Nor can the migration verify its own work with a ``SELECT``:
under the same policy every count comes back ``0``, so a post-condition
check would *pass* on exactly the databases where the backfill did
nothing.

DDL is the escape hatch. ``ALTER TABLE ... ALTER COLUMN ... TYPE ...
USING <expr>`` rewrites every heap tuple in the relation: row-level
security qualifies DML (``SELECT``/``INSERT``/``UPDATE``/``DELETE``),
never a table rewrite. So the value transformation reaches all rows
regardless of the GUC, needs no ``BYPASSRLS``, no ``SET ROLE`` and no
temporary ``NO FORCE ROW LEVEL SECURITY`` window (which would briefly
disarm the table's isolation mid-migration). This is the same reasoning
as ``a4e91c7d2b58``, which gave ``price_observations.attempt_uuid`` a
volatile ``server_default`` *during* its rewrite for exactly this
reason.

The rewrite is also self-verifying, which the ``UPDATE`` form was not:
a value the ``CASE`` fails to normalise cannot be cast and aborts the
migration loudly, and ``SET NOT NULL`` re-scans the whole relation as
DDL (again RLS-exempt) and fails if a single row is still NULL.

The transformation each row receives:

* a value that already parses as a UUID (Scrapyd 1.6 mints
  ``uuid1().hex`` — 32 hex chars, no dashes, which Postgres accepts as a
  ``uuid`` literal) is cast through unchanged, so a CONFIRMED row keeps
  naming the run it actually named;
* anything else — NULL (a PLANNED/FAILED row that never got an answer),
  or a hand-written value — takes the row's own ``intent_id``. That is
  deterministic per row, unique by construction (it is the primary key),
  and is exactly the id the pre-B2 ``SCRAPYD_DETERMINISTIC_JOBID`` path
  would have POSTed for it, so an in-flight row keeps the name it was
  sent under.

New rows get the ``uuid5`` value instead; the two schemes coexist without
ambiguity because the id's only requirement is to be stable per identity,
not to be derivable by one particular formula. No row is deleted, no
statement is issued that RLS could filter, and no partition is touched.

Reversible: ``downgrade`` widens ``scrapyd_job_id`` back to a nullable
``TEXT`` (lossless — every uuid renders as text) and drops ``node_url``.
Dropping ``node_url`` loses the recorded node for every in-flight intent,
so a downgrade must only be run on a deployment that has also reverted
the code.

Hand-authored (matches ``app_shared.models.dispatch`` exactly) — this
build environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6e5d1c94a72'
down_revision: Union[str, Sequence[str], None] = 'a4e91c7d2b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: A canonical UUID, with or without its dashes. Postgres accepts both
#: spellings as ``uuid`` input, and Scrapyd's own ``uuid1().hex`` is the
#: undashed one, so the guard has to admit it too.
#:
#: Deliberately free of backslashes: this pattern is rendered as a SQL
#: string literal in Alembic's ``--sql`` (offline) mode, where a
#: backslash's meaning depends on ``standard_conforming_strings``. The
#: brace-wrapped ``{uuid}`` spelling Postgres also accepts is therefore
#: NOT matched here — such a value simply takes the ``intent_id``
#: fallback, which is strictly safer than a cast that might not parse.
_UUID_RE = (
    "^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    "[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)

#: The ``USING`` expression of the ``TYPE uuid`` rewrite, which is where
#: this migration's backfill lives (see the module docstring: an
#: ``UPDATE`` would be silently filtered to zero rows by the table's
#: fail-closed RLS policy, a table rewrite is not). Every branch yields
#: a non-NULL ``uuid``, so the ``SET NOT NULL`` that follows is a
#: re-scan that confirms the rewrite rather than a step that can trip
#: over rows the backfill missed.
_BACKFILL_USING = (
    "CASE WHEN scrapyd_job_id ~ '" + _UUID_RE + "' "
    "THEN scrapyd_job_id::uuid ELSE intent_id END"
)


def upgrade() -> None:
    """Upgrade schema: node_url, and a deterministic NOT NULL scrapyd_job_id."""
    op.add_column(
        "dispatch_intents",
        sa.Column(
            "node_url",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )

    # The backfill IS the rewrite — see the module docstring. A separate
    # `UPDATE dispatch_intents` would be filtered to zero rows by this
    # table's FORCE-RLS fail-closed policy under the NOBYPASSRLS
    # migration role and silently do nothing; the USING expression of a
    # type change is DDL and reaches every row. A row whose recorded id
    # is a real UUID keeps it; everything else adopts its own primary
    # key, which is what the pre-B2 deterministic-jobid path would have
    # POSTed for it.
    op.alter_column(
        "dispatch_intents",
        "scrapyd_job_id",
        existing_type=sa.Text(),
        type_=sa.Uuid(as_uuid=True),
        postgresql_using=_BACKFILL_USING,
        nullable=False,
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema: back to a nullable TEXT jobid; drop node_url."""
    op.alter_column(
        "dispatch_intents",
        "scrapyd_job_id",
        existing_type=sa.Uuid(as_uuid=True),
        type_=sa.Text(),
        postgresql_using="scrapyd_job_id::text",
        nullable=True,
        existing_nullable=False,
    )
    op.drop_column("dispatch_intents", "node_url")
