"""scrape_job_targets.dispatch_intent_id + FK to dispatch_intents

Revision ID: c9a271f5b6e8
Revises: a3f5c81d7e46
Create Date: 2026-08-25 00:00:00.000000

EPA B6 folded-in item 1 (2026-08-25): B2's owed migration.

B2 (SPEC-10/READY-002 dispatch-stamping) added
``app_shared.models.jobs.ScrapeJobTarget.dispatch_intent_id`` as a
**model-only** attribute — B1's migration (``e7b21f3a8c94``) created
``dispatch_intents`` but did not add this column, because the migration
lane was held by a concurrent worker at the time. B2's task report
recorded the gap explicitly (see ``ScrapeJobTarget.dispatch_intent_id``'s
docstring in ``libs/shared/app_shared/models/jobs.py``). This migration
closes it: the column now exists in the schema, matching the model
exactly (nullable ``Uuid``, indexed).

FK target: ``dispatch_intents.intent_id`` — that is the table's real
primary-key **column** (``app_shared.models.dispatch.DispatchIntent``
declares it ``id: Mapped[uuid.UUID] = mapped_column("intent_id", ...)``,
with ``intent_id = synonym("id")`` so both spellings work in ORM query
code; the DDL/FK layer only knows the physical column name).

``ondelete="SET NULL"`` — mirrors the existing
``fk_sjt_current_strategy_method_dsm`` FK on this same table (also a
nullable "which attempt-scoped row produced/drove this target" pointer).
``dispatch_intent_id`` is populated for traceability
(``app.workers.tasks_dispatch.stamp_targets_dispatched``, B2) after the
fact — it is not part of any invariant a target's own lifecycle depends
on, so losing the pointer when its intent is removed must never cascade
into losing the target itself. Nothing in this repo deletes
``dispatch_intents`` rows in the ordinary run path (B1: durable audit
record of an intended dispatch), so ``SET NULL`` is a safety net for an
operator cleanup, not a live semantics decision.

RLS: unaffected. ``scrape_job_targets`` already carries its own
``workspace_id`` + composite FK to ``scrape_jobs`` and already has its
RLS policy (``a6b0234cd4ad`` + ``f2a6c1d80b37``'s partition sweep) — a
plain nullable column addition needs no new policy.

Reversible: ``downgrade`` drops the FK, the index, then the column —
losing the traceability pointer only (no data referenced elsewhere).

Hand-authored (matches ``app_shared.models.jobs.ScrapeJobTarget``
exactly) — this build environment has no live Postgres connection for
autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9a271f5b6e8'
down_revision: Union[str, Sequence[str], None] = 'a3f5c81d7e46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: scrape_job_targets.dispatch_intent_id (+index, +FK)."""
    op.add_column(
        "scrape_job_targets",
        sa.Column("dispatch_intent_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_scrape_job_targets_dispatch_intent_id",
        "scrape_job_targets",
        ["dispatch_intent_id"],
    )
    op.create_foreign_key(
        "fk_sjt_dispatch_intent_id_dispatch_intents",
        "scrape_job_targets",
        "dispatch_intents",
        ["dispatch_intent_id"],
        ["intent_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema: drop the FK, index, then the column."""
    op.drop_constraint(
        "fk_sjt_dispatch_intent_id_dispatch_intents",
        "scrape_job_targets",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_scrape_job_targets_dispatch_intent_id", table_name="scrape_job_targets"
    )
    op.drop_column("scrape_job_targets", "dispatch_intent_id")
