"""Job-creation service (`contracts/job-service.md`, FR-006, FR-007, FR-010, FR-020).

Pure-ish orchestration — SQLAlchemy + the `app_shared.messaging` enqueue
seam only (no scrapy/twisted/fastapi). The API router
(`apps/api/app/routers/jobs.py`) resolves the match/variant in-workspace
(`scoped_get`) and delegates job/target creation here, so creation logic
is unit-testable against a fake session + fake `enqueue`.

`create_match_job` (US1) creates a single-target `scope=MATCH` job.
`create_variant_job` (US2) fans a `scope=VARIANT` job out to one target
per ACTIVE match of the variant, resolving to an immediate COMPLETED
job with no dispatch when the variant has zero active matches.

`create_scope_job` (SPEC-13 US2, `contracts/job-service-seam.md`) is the
new scope-general entry point reused by the scheduler's refresh pass
(and future manual scope-run endpoints): it resolves ACTIVE matches for
any of the six `ScrapeScope` members via
`app_shared.jobs.scopes.resolve_scope_matches`, and — unlike
`create_match_job`/`create_variant_job` — returns `(None, None)` with no
job/dispatch at all when zero matches resolve (FR-015), rather than an
immediate `COMPLETED` job. `create_match_job`/`create_variant_job`
themselves are left untouched.

Every read/write is workspace-scoped; the session already carries RLS
context set by the caller (the router's auth seam) or — for the
scheduler's `create_scope_job` calls — is the caller's own transaction
on the BYPASSRLS system session (`app_shared.database.get_system_session`),
with workspace scoping enforced at the application layer instead
(`scoped_select`/explicit `workspace_id=` on every insert). Counters
start at 0 and are only ever set by `app_shared.jobs.targets.aggregate_counts`
— this module never increments them. This module does not call Scrapyd —
it only creates rows and enqueues the dispatch task, and it never
commits — the caller owns the transaction (enqueue-before-commit,
FR-012).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app_shared.enums import (
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.scopes import resolve_scope_matches
from app_shared.messaging import enqueue
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.repository import scoped_select
from app_shared.task_names import SCRAPE_DISPATCH_JOB

__all__ = ["create_match_job", "create_variant_job", "create_scope_job"]


def _enqueue_dispatch(job_id: uuid.UUID, workspace_id: uuid.UUID | str) -> None:
    enqueue(
        SCRAPE_DISPATCH_JOB,
        queue="scrape_dispatch",
        kwargs={"scrape_job_id": str(job_id), "workspace_id": str(workspace_id)},
    )


def create_match_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    match: CompetitorProductMatch,
    requested_by: uuid.UUID | None,
) -> tuple[uuid.UUID, ScrapeJobStatus]:
    """Create a `scope=MATCH` job with exactly one target, then enqueue dispatch.

    Precondition: `match` is already resolved in-workspace by the
    router (`scoped_get`). Returns `(job.id, ScrapeJobStatus.PENDING)`.
    """
    now = datetime.now(timezone.utc)

    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        product_id=match.product_id,
        product_variant_id=match.product_variant_id,
        competitor_id=match.competitor_id,
        match_id=match.id,
        status=ScrapeJobStatus.PENDING,
        total_targets=1,
        requested_by=requested_by,
        source=ScrapeJobSource.API,
        created_at=now,
    )
    session.add(job)
    session.flush()

    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=match.id,
        status=ScrapeTargetStatus.PENDING,
        created_at=now,
    )
    session.add(target)
    session.flush()

    _enqueue_dispatch(job.id, workspace_id)

    return job.id, ScrapeJobStatus.PENDING


def create_scope_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    scope: ScrapeScope,
    target_id: uuid.UUID | None,
    requested_by: uuid.UUID | None,
    job_type: ScrapeJobType = ScrapeJobType.MANUAL,
    source: ScrapeJobSource = ScrapeJobSource.API,
) -> tuple[uuid.UUID | None, ScrapeJobStatus | None]:
    """Create a job for any of the six ``ScrapeScope`` members, or none.

    Resolves ``matches = resolve_scope_matches(session, workspace_id=...,
    scope=..., target_id=...)``. If ``matches`` is empty, creates **no**
    job and enqueues **no** dispatch — returns ``(None, None)`` (FR-015;
    unlike ``create_variant_job``'s zero-match case, which still creates
    an immediately-``COMPLETED`` job). Otherwise creates one
    ``ScrapeJob(type=job_type, source=source, scope=scope,
    status=PENDING, total_targets=len(matches))``, flushes, then one
    ``ScrapeJobTarget(status=PENDING)`` per match, then enqueues dispatch
    **before returning** — this function never commits; the caller
    (the scheduler's per-rule transaction, or a future manual scope-run
    endpoint) owns the transaction boundary (enqueue-before-commit,
    FR-012/FR-014).

    The scheduler calls this with ``job_type=ScrapeJobType.SCHEDULED,
    source=ScrapeJobSource.SCHEDULER``. ``create_match_job``/
    ``create_variant_job`` are unaffected by this addition.
    """
    matches = resolve_scope_matches(
        session, workspace_id=workspace_id, scope=scope, target_id=target_id
    )
    if not matches:
        return None, None

    now = datetime.now(timezone.utc)

    job = ScrapeJob(
        workspace_id=workspace_id,
        type=job_type,
        scope=scope,
        product_id=target_id if scope == ScrapeScope.PRODUCT else None,
        product_variant_id=target_id if scope == ScrapeScope.VARIANT else None,
        product_group_id=target_id if scope == ScrapeScope.PRODUCT_GROUP else None,
        competitor_id=target_id if scope == ScrapeScope.COMPETITOR else None,
        match_id=target_id if scope == ScrapeScope.MATCH else None,
        status=ScrapeJobStatus.PENDING,
        total_targets=len(matches),
        requested_by=requested_by,
        source=source,
        created_at=now,
    )
    session.add(job)
    session.flush()

    for match in matches:
        target = ScrapeJobTarget(
            workspace_id=workspace_id,
            scrape_job_id=job.id,
            match_id=match.id,
            status=ScrapeTargetStatus.PENDING,
            created_at=now,
        )
        session.add(target)
    session.flush()

    _enqueue_dispatch(job.id, workspace_id)

    return job.id, ScrapeJobStatus.PENDING


def create_variant_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    variant: ProductVariant,
    requested_by: uuid.UUID | None,
) -> tuple[uuid.UUID, ScrapeJobStatus]:
    """Create a `scope=VARIANT` job fanned out to one target per ACTIVE match.

    Precondition: `variant` is already resolved in-workspace by the
    router (`scoped_get`). Resolves all **ACTIVE** matches of the
    variant via one `scoped_select` (inactive matches excluded, US2-AS2)
    and creates exactly one `ScrapeJobTarget` per active match
    (`unique(scrape_job_id, match_id)` guards duplicates at the DB
    layer). A variant with zero active matches still creates the job,
    but resolves it to `COMPLETED` immediately with `total_targets=0`
    and no dispatch (FR-020, US2-AS4).

    Returns `(job.id, status)` where `status` is `PENDING` (N > 0) or
    `COMPLETED` (N == 0).
    """
    now = datetime.now(timezone.utc)

    active_matches = (
        session.execute(
            scoped_select(CompetitorProductMatch, workspace_id).where(
                CompetitorProductMatch.product_variant_id == variant.id,
                CompetitorProductMatch.status == MatchStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    total_targets = len(active_matches)

    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        product_id=variant.product_id,
        product_variant_id=variant.id,
        status=ScrapeJobStatus.PENDING,
        total_targets=total_targets,
        requested_by=requested_by,
        source=ScrapeJobSource.API,
        created_at=now,
    )
    session.add(job)
    session.flush()

    if total_targets == 0:
        job.status = ScrapeJobStatus.COMPLETED
        job.completed_at = now
        return job.id, ScrapeJobStatus.COMPLETED

    for match in active_matches:
        target = ScrapeJobTarget(
            workspace_id=workspace_id,
            scrape_job_id=job.id,
            match_id=match.id,
            status=ScrapeTargetStatus.PENDING,
            created_at=now,
        )
        session.add(target)
    session.flush()

    _enqueue_dispatch(job.id, workspace_id)

    return job.id, ScrapeJobStatus.PENDING
