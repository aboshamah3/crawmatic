"""dispatch_intents table + scrape_jobs.planning_generation

Revision ID: e7b21f3a8c94
Revises: c1d7a4e9b350
Create Date: 2026-08-25 00:00:00.000000

EPA B1 (2026-08-25, READY-002, fixes P0.4A): the durable half of dispatch
idempotency.

Two things land here.

1. ``dispatch_intents`` — one row per intended Scrapyd dispatch, written
   by the planner in the SAME transaction that advances the strategy
   cursor. Before B1 the only record that a batch had been POSTed was a
   Redis key ``dispatched:{scrape_job_id}:{batch_index}`` with a 900 s
   TTL: a *cache* being used as an *authority*. When it expired the
   system forgot; and because ``batch_index`` is a position in a plan
   that is re-derived on every delivery, the same key came to mean
   different work the moment the target set changed shape — which is how
   a replanned subset got answered with the previous batch's jobid and
   was never dispatched at all.

   ``identity_key`` is UNIQUE, not merely indexed: the database is what
   guarantees one intent per canonical identity, so two concurrent
   planners cannot both create one and both POST.

   ``batch_index`` survives as a nullable text column for traceability
   only — it is deliberately absent from ``identity_key`` and from every
   idempotency decision.

2. ``scrape_jobs.planning_generation`` (int, NOT NULL, server_default
   ``0``) — **the durable slot the dispatch identity's
   ``planning_generation`` reads from**. It advances only inside a
   transaction that commits an explicit planning transition (a strategy
   chain advance in ``dispatch_job``, or a stall re-plan in
   ``recover_stalled_batches``), never per task run. That is what makes a
   Celery replay rebuild the *same* identity and produce one POST instead
   of two. ``server_default='0'`` (not merely a Python-side default) so
   every existing row, and any writer that predates the column, lands on
   "never planned".

RLS: the standard, direct :func:`app_shared.models.rls.emit_rls_policy`
in this same migration (§32, Principle II) — ``dispatch_intents`` carries
its own ``workspace_id`` and a composite FK
``(workspace_id, scrape_job_id) -> scrape_jobs(workspace_id, id)``, the
:class:`~app_shared.models.jobs.ScrapeJobTarget` shape. It is
deliberately NOT the transitive/``EXISTS``-via-parent shape used by
``strategy_attempt_stats``/``match_audit_classifications``: this table is
read on the hot dispatch path and by cancellation, both of which already
know their workspace, so a direct predicate is an index lookup where the
transitive one is a correlated subquery per row; the composite FK makes a
cross-workspace intent->job reference structurally impossible rather than
merely filtered; and keeping a real ``workspace_id`` keeps every call
site visible to ``scripts/check_workspace_scoping.py``. See
``app_shared.models.dispatch``'s module docstring.

**No DDL for the new enum.** ``DispatchIntentState`` is an app-validated
``VARCHAR(32)`` (``app_shared.enums.enum_column`` ->
``_AppValidatedEnumString``), never a Postgres-native ``ENUM`` — the same
convention every status column in this repo follows (see ``a6b0234cd4ad``
and the note in ``c1d7a4e9b350``). There is nothing to ``CREATE TYPE``.

Reversible: ``downgrade`` drops the table (its RLS policy goes with it)
and the ``planning_generation`` column. Dropping them loses the durable
dispatch record and resets every job to "never planned", so a downgrade
must only ever be run on a deployment that has also reverted the code.

Hand-authored (matches ``app_shared.models.dispatch`` exactly) — this
build environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app_shared.models import emit_rls_policy

# revision identifiers, used by Alembic.
revision: str = 'e7b21f3a8c94'
down_revision: Union[str, Sequence[str], None] = 'c1d7a4e9b350'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: dispatch_intents (+RLS) and the planning generation."""
    op.add_column(
        "scrape_jobs",
        sa.Column(
            "planning_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.create_table(
        "dispatch_intents",
        sa.Column("intent_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scrape_job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("planning_generation", sa.Integer(), nullable=False),
        sa.Column("strategy_method", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("node_class", sa.Text(), nullable=False),
        sa.Column("match_ids_digest", sa.Text(), nullable=False),
        sa.Column(
            "match_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("identity_payload", sa.Text(), nullable=False),
        sa.Column("scrapyd_job_id", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "cancellation_generation_at_creation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("batch_index", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("intent_id", name="pk_dispatch_intents"),
        # One intent per canonical identity — enforced by the DATABASE, so
        # two concurrent planners cannot both create one and both POST.
        sa.UniqueConstraint("identity_key", name="uq_dispatch_intents_identity_key"),
        # Workspace-local composite FK to the parent job: a
        # cross-workspace intent -> job reference is structurally
        # impossible, not merely app-filtered (the ScrapeJobTarget /
        # SPEC-05 precedent).
        sa.ForeignKeyConstraint(
            ["workspace_id", "scrape_job_id"],
            ["scrape_jobs.workspace_id", "scrape_jobs.id"],
            name="fk_dispatch_intents_workspace_scrape_job_scrape_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_dispatch_intents_workspace_id_workspaces",
        ),
    )
    op.create_index(
        "ix_dispatch_intents_workspace_id", "dispatch_intents", ["workspace_id"]
    )
    op.create_index(
        "ix_dispatch_intents_scrape_job_id", "dispatch_intents", ["scrape_job_id"]
    )
    # Cancellation step 2 reads "every Scrapyd job id for this job"; the
    # reconcile path looks a jobid back up. Both are covered here.
    op.create_index(
        "ix_dispatch_intents_scrapyd_job_id", "dispatch_intents", ["scrapyd_job_id"]
    )

    # RLS in the SAME migration that creates the table (§32, Principle II).
    for statement in emit_rls_policy("dispatch_intents"):
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema: drop dispatch_intents and the planning generation."""
    op.drop_index("ix_dispatch_intents_scrapyd_job_id", table_name="dispatch_intents")
    op.drop_index("ix_dispatch_intents_scrape_job_id", table_name="dispatch_intents")
    op.drop_index("ix_dispatch_intents_workspace_id", table_name="dispatch_intents")
    op.drop_table("dispatch_intents")
    op.drop_column("scrape_jobs", "planning_generation")
