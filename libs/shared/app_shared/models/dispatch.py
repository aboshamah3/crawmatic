"""``dispatch_intents`` — the durable record of one intended Scrapyd dispatch.

EPA B1 (READY-002, fixes P0.4A). Per ``contracts/models-jobs.md``'s
conventions: workspace-owned, on
:class:`~app_shared.models.base.WorkspaceScopedBase`, registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS`, and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_dispatch_intents_table.py``), not
here — this module only declares ORM shape.

Why the table exists
--------------------
Before B1 the *only* record that a batch had been POSTed was a Redis key
``dispatched:{scrape_job_id}:{batch_index}`` with a 900 s TTL. That is a
cache, and it was being used as an authority: when it expired the system
forgot; when the plan changed shape the same key silently came to mean
different work. This table is the authority the guard was pretending to
be — a row per intended dispatch, written by the planner in the same
transaction that advances the strategy cursor, and carrying the identity
that names it.

Scoping decision (RLS)
----------------------
**Own ``workspace_id`` column + a composite FK to the parent job**, i.e.
exactly the :class:`~app_shared.models.jobs.ScrapeJobTarget` shape, and
NOT the transitive/``EXISTS``-via-parent shape used by
``strategy_attempt_stats``/``match_audit_classifications``. Three reasons,
all specific to this table:

1. A dispatch intent is read on the **hot dispatch path** and on the
   cancellation path, both of which already know their workspace. A
   direct ``workspace_id = current_setting(...)`` policy is an index
   lookup; the transitive policy is a correlated subquery per row.
2. ``(workspace_id, scrape_job_id) -> scrape_jobs(workspace_id, id)``
   makes a cross-workspace intent -> job reference **structurally
   impossible** (research D4, the SPEC-05 precedent), rather than merely
   filtered. The transitive shape cannot express that.
3. It keeps the table queryable through
   :func:`app_shared.repository.scoped_select`, so the CI scoping guard
   (``scripts/check_workspace_scoping.py``) can see and enforce every
   call site — which the no-workspace-column tables are exempt from by
   construction.

``identity_key`` is UNIQUE (not merely indexed): the *database* is what
guarantees one intent per canonical identity, so two concurrent planners
cannot both create one and both POST. ``match_ids`` is stored alongside
``match_ids_digest`` deliberately — the digest is what the identity is
built from, but an operator investigating a wedged job needs to see the
actual work, and re-deriving it from a plan that no longer exists is not
possible.

``TimestampMixin`` (``created_at``/``updated_at``) rather than the
jobs tables' created-at-only shape: a row's ``state`` genuinely mutates
(PLANNED -> POSTED -> CONFIRMED), so "when did this last move" is a real
question here in a way it is not for a target.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Integer, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app_shared.enums import DispatchIntentState, ScrapeProfileMode, enum_column
from app_shared.ids import new_uuid7
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class DispatchIntent(Base, WorkspaceScopedBase, TimestampMixin):
    """``dispatch_intents`` — one intended Scrapyd dispatch, durably named."""

    __tablename__ = "dispatch_intents"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_dispatch_intents_identity_key"),
        ForeignKeyConstraint(
            ["workspace_id", "scrape_job_id"],
            ["scrape_jobs.workspace_id", "scrape_jobs.id"],
            name="fk_dispatch_intents_workspace_scrape_job_scrape_jobs",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_dispatch_intents_workspace_id_workspaces",
        ),
    )

    #: The PK column is spelled ``intent_id`` in the table (and in the
    #: guard JSON), but is the ordinary ``Base.id`` UUIDv7 attribute so
    #: every generic helper (``scoped_get``, the test session double, the
    #: repository guard) keeps working unchanged. ``intent_id`` below is a
    #: real SQLAlchemy synonym, so both spellings work in queries and in
    #: attribute access.
    id: Mapped[uuid.UUID] = mapped_column(
        "intent_id", Uuid(as_uuid=True), primary_key=True, default=new_uuid7
    )
    intent_id = synonym("id")

    scrape_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )

    # --- the canonical identity, decomposed ---------------------------------
    #
    # Stored column-per-component rather than as the opaque payload alone
    # so the table answers operational questions ("what did we dispatch
    # for this domain under generation 3?") without string-parsing.
    planning_generation: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    strategy_method: Mapped[str] = mapped_column(Text(), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    mode: Mapped[ScrapeProfileMode] = enum_column(ScrapeProfileMode, nullable=False)
    node_class: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``sha256`` over the sorted ``match_ids``, first 16 hex chars — the
    #: identity's work component.
    match_ids_digest: Mapped[str] = mapped_column(Text(), nullable=False)
    #: The actual work, for humans. See the module docstring.
    match_ids: Mapped[list] = mapped_column(JSONB(), nullable=False, default=list)
    #: ``DispatchIdentity.key`` — UNIQUE, so the database (not application
    #: discipline) enforces one intent per identity.
    identity_key: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``DispatchIdentity.canonical_payload`` — kept verbatim so a stored
    #: record can be compared against a freshly-built identity without
    #: re-deriving it from the columns above.
    identity_payload: Mapped[str] = mapped_column(Text(), nullable=False)

    # --- outcome -------------------------------------------------------------
    scrapyd_job_id: Mapped[str | None] = mapped_column(Text(), nullable=True, index=True)
    state: Mapped[DispatchIntentState] = enum_column(
        DispatchIntentState, nullable=False, default=DispatchIntentState.PLANNED
    )
    #: A2's fence, captured at plan time: the ``scrape_jobs.
    #: cancellation_generation`` this work was authorized under. Dispatch
    #: refuses the intent when the job has since moved past it.
    cancellation_generation_at_creation: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )
    #: The spider's ``batch_index`` argument. Recorded for traceability
    #: ONLY — it is deliberately absent from ``identity_key`` and from
    #: every idempotency decision (that positional key is the P0.4A bug).
    batch_index: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Why a ``FAILED`` row failed. NULL on every other state.
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
