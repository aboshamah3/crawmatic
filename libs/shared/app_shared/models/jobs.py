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

from sqlalchemy import ForeignKeyConstraint, Integer, UniqueConstraint, Uuid
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
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
