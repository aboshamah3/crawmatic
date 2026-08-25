"""Jobs & orchestration ORM models: scrape_jobs, scrape_job_targets (SPEC-08).

Per ``contracts/models-jobs.md`` / ``data-model.md`` — two workspace-owned
tables, both on :class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_scrape_jobs_targets_tables.py``), not
here — this module only declares ORM shape.

* :class:`ScrapeJob` — the job header for one triggered scraping run at a
  given scope (MATCH/VARIANT in this spec; the remaining ``ScrapeScope``
  members are forward-compat for later scope-run endpoints owned by
  SPEC-13). Single-column PK (``id``) **plus** ``unique(workspace_id,
  id)`` so :class:`ScrapeJobTarget` can composite-FK its parent job
  workspace-locally (same pattern SPEC-05 used for
  ``competitors``/``competitor_product_matches``). Counters
  (``success_count``/``failure_count``/``skipped_count``) are only ever
  **overwritten** by ``aggregate_counts`` (``app_shared.jobs.targets``),
  never per-target incremented (FR-018).
* :class:`ScrapeJobTarget` — one match to be scraped within a job.
  ``unique(scrape_job_id, match_id)`` guarantees one target per match per
  job (the arbiter for the set-based target insert). Composite-FKs its
  parent job workspace-locally (``(workspace_id, scrape_job_id) ->
  scrape_jobs(workspace_id, id)``), so a cross-workspace target->job
  reference is structurally impossible, not just app-filtered (research
  D4, the SPEC-05 ``competitor_product_matches`` precedent).

Both tables carry **``created_at`` only** (no ``updated_at``, §22) — an
explicit ``created_at`` column declared directly (not via
``TimestampMixin``), matching the ``RefreshToken``/``ProductGroupItem``
precedent: the value is supplied by the caller at row-creation time (see
``app_shared.jobs.service``), not defaulted at the column/DDL level.

Scope refs on the job (``product_id``/``product_variant_id``/
``product_group_id``/``competitor_id``/``match_id``) and
``ScrapeJobTarget.match_id`` are **soft** references (plain
indexed/nullable ``Uuid`` columns, no FK) — matching §22's soft-reference
philosophy and the SPEC-07 observations precedent (a match may be
archived/deleted without cascading job history).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Integer, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    MatchPriority,
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
    enum_column,
)
from app_shared.models.base import Base, TZDateTime, WorkspaceScopedBase


class ScrapeJob(Base, WorkspaceScopedBase):
    """``scrape_jobs`` — job header for one triggered scraping run.

    ``created_at`` only (no ``updated_at``, §22) — declared directly, not
    via ``TimestampMixin``.
    """

    __tablename__ = "scrape_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_scrape_jobs_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_scrape_jobs_workspace_id_workspaces",
        ),
    )

    type: Mapped[ScrapeJobType] = enum_column(ScrapeJobType, nullable=False)
    scope: Mapped[ScrapeScope] = enum_column(ScrapeScope, nullable=False)

    # Soft scope refs (no FK, §22) — set according to `scope`.
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    product_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    product_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    competitor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    status: Mapped[ScrapeJobStatus] = enum_column(
        ScrapeJobStatus, nullable=False, default=ScrapeJobStatus.PENDING
    )
    priority: Mapped[MatchPriority] = enum_column(
        MatchPriority, nullable=False, default=MatchPriority.NORMAL
    )

    # Aggregate counters — ONLY ever overwritten by
    # `app_shared.jobs.targets.aggregate_counts`, never per-target
    # incremented (FR-018).
    total_targets: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[ScrapeJobSource] = enum_column(ScrapeJobSource, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    #: EPA A2 cancellation fence. Bumped (and the status set to
    #: ``CANCELLED``) FIRST, inside the cancellation transaction, before
    #: any Redis/Scrapyd/reservation work is attempted. Dispatch (B1/B2)
    #: and the result-persistence path treat a generation older than this
    #: as "authorized before the cancellation" and reject the work, so a
    #: crash anywhere in the out-of-band steps can never leave a late
    #: result contradicting a committed ``CANCELLED``. ``0`` == never
    #: cancelled.
    cancellation_generation: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )

    #: EPA B1 (READY-002). **The durable slot for the dispatch identity's
    #: ``planning_generation``** — i.e. this job's strategy-chain plan
    #: version. It advances ONLY inside the transaction that commits an
    #: explicit planning state transition:
    #:
    #: * ``dispatch_job`` advancing a target's strategy cursor (a chain
    #:   selection or fallback), and
    #: * ``recover_stalled_batches`` re-planning a stalled batch.
    #:
    #: It is deliberately NOT bumped per task run, per delivery, or per
    #: retry: a Celery replay re-reads this value and therefore rebuilds
    #: the *same* :class:`~app_shared.scrapyd.identity.DispatchIdentity`,
    #: which is the whole reason a duplicate delivery produces one POST
    #: instead of two. Minting it per run would make every retry look like
    #: new work.
    #:
    #: A rolled-back attempt takes its bump with it, so the retry
    #: re-derives the identical generation — the counter measures
    #: *committed* plans, not attempts. ``0`` == never planned.
    planning_generation: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)


class ScrapeJobTarget(Base, WorkspaceScopedBase):
    """``scrape_job_targets`` — one match to be scraped within a job.

    ``created_at`` only (no ``updated_at``, §22) — declared directly, not
    via ``TimestampMixin``.
    """

    __tablename__ = "scrape_job_targets"
    __table_args__ = (
        UniqueConstraint(
            "scrape_job_id",
            "match_id",
            name="uq_scrape_job_targets_scrape_job_id_match_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scrape_job_id"],
            ["scrape_jobs.workspace_id", "scrape_jobs.id"],
            name="fk_scrape_job_targets_workspace_scrape_job_scrape_jobs",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_scrape_job_targets_workspace_id_workspaces",
        ),
        ForeignKeyConstraint(
            ["current_strategy_method_id"],
            ["domain_strategy_methods.id"],
            name="fk_sjt_current_strategy_method_dsm",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["dispatch_intent_id"],
            ["dispatch_intents.intent_id"],
            name="fk_sjt_dispatch_intent_id_dispatch_intents",
            ondelete="SET NULL",
        ),
    )

    scrape_job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Soft ref to competitor_product_matches (no FK, §22 / SPEC-07
    # precedent) — a match may be archived/deleted without cascading job
    # history; workspace consistency of the match is enforced at
    # creation time in the service.
    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)

    status: Mapped[ScrapeTargetStatus] = enum_column(
        ScrapeTargetStatus, nullable=False, default=ScrapeTargetStatus.PENDING
    )

    # Set by the in-flight lock (SPEC-11); read by stall recovery to skip
    # locked-but-live matches.
    locked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    # Last scrapyd POST for this target (F-2): dispatch selection + per-target stall aging key. NULL = never dispatched.
    dispatched_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: EPA B2 (2026-08-25). Set together with `dispatched_at`, by
    #: `app.workers.tasks_dispatch.stamp_targets_dispatched` ONLY, from
    #: the `intent_id` of the exact-identity-matching committed dispatch
    #: (`app_shared.scrapyd.identity.get_committed_dispatch`, B1). NULL
    #: for every target dispatched before B2 and for any target whose
    #: committed dispatch carried no intent (the legacy Redis-only guard
    #: shape). EPA B6 (2026-08-25) added the column + FK B2 could not:
    #: B1's migration (`e7b21f3a8c94`) landed before this one because the
    #: migration lane was held by a concurrent worker at the time; the FK
    #: (`fk_sjt_dispatch_intent_id_dispatch_intents`, `ondelete="SET
    #: NULL"`, mirroring `fk_sjt_current_strategy_method_dsm`) now exists,
    #: added by
    #: `alembic/versions/c9a271f5b6e8_scrape_job_targets_dispatch_intent_fk.py`.
    dispatch_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)

    # Durable strategy-chain cursor.  A target can hand off between HTTP
    # and browser nodes without losing which versioned candidate is next;
    # the token also makes duplicate deliveries distinguishable from a new
    # chain for the same job/match.
    current_strategy_method_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    chain_token: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    strategy_attempt_ordinal: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )
    strategy_url_override: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # EPA A2: the audit trail of an administrative cancellation. All three
    # are written together (and only) by `mark_target` on the transition to
    # `ScrapeTargetStatus.CANCELLED`; NULL means "this target was never
    # cancelled". They exist so a target closed without a result is
    # distinguishable from a real outcome by a recorded human decision,
    # not just by its status string.
    cancelled_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
