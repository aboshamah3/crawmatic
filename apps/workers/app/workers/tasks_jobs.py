"""`scrape_dispatch` + `maintenance` queue tasks (SPEC-08 US1/US3).

`dispatch_job` — the `scrape_dispatch`-queue Celery task that expands a
job into Scrapyd runs. Thin orchestrator over the pure
`app_shared.jobs.batching`/`nodes` logic + the reused SPEC-07
`ScrapydDispatchClient`. Relies on the existing `worker_process_init` ->
`dispose_engine` fork-safety hook (`celery_app.py`, FR-016) — never
starts Scrapy in-process (Principle V).

`finalize_jobs`/`refresh_job_counters` (`contracts/lifecycle-counters.md`,
D5/D6, US3) aggregate `scrape_job_targets` counts onto the job row in one
UPDATE per job (never a per-target increment) and finalize a job's status
deterministically once all its targets are terminal.

`recover_stalled_batches` (`contracts/stall-recovery.md`, D4, US3) detects
a batch dispatched to a node that died — its targets never left PENDING —
past `SCRAPE_STALL_TIMEOUT_SECONDS`, and re-dispatches only those
still-unprogressed, un-locked targets under a stall-window-bucketed
`batch_index` so the reused Redis `SET NX` guard still neutralizes a
duplicate recovery delivery within one window.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.database import get_session, set_workspace_context
from app_shared.enums import ScrapeJobStatus, ScrapeProfileMode, ScrapeTargetStatus
from app_shared.jobs.batching import ResolvedTarget, plan_batches
from app_shared.jobs.lifecycle import resolve_finalized_status, stall_window
from app_shared.jobs.nodes import select_node
from app_shared.jobs.targets import Counts, aggregate_counts
from app_shared.messaging import enqueue
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.repository import scoped_get, scoped_select
from app_shared.scrapyd import ScrapydDispatchClient
from app_shared.task_names import (
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    STRATEGY_STATS_FLUSH,
)

# The Scrapy project + spider deployed to the Scrapyd nodes (apps/scrapers) —
# unchanged from the SPEC-07 thin `dispatch.generic_price_spider` task.
_SCRAPYD_PROJECT = "price_monitor"
_GENERIC_PRICE_SPIDER = "generic_price_spider"

# The Scrapy project + spider deployed to the *browser* Scrapyd nodes
# (apps/scrapers-browser, SPEC-14 US1) — a BROWSER-mode batch must be
# scheduled here, never against the HTTP project/spider above
# (contracts/dispatch-routing.md, US2).
_SCRAPYD_BROWSER_PROJECT = "price_monitor_browser"
_GENERIC_BROWSER_SPIDER = "generic_browser_price_spider"

# A job in one of these statuses has already finalized — a duplicate/
# retried dispatch delivery must never re-open it (idempotent RUNNING+
# started_at transition, contract step 2).
_TERMINAL_JOB_STATUSES = frozenset(
    {
        ScrapeJobStatus.COMPLETED,
        ScrapeJobStatus.PARTIAL_FAILED,
        ScrapeJobStatus.FAILED,
        ScrapeJobStatus.CANCELLED,
    }
)

# `finalize_jobs`/`refresh_job_counters` scan every job not yet finalized —
# `PENDING` (dispatch hasn't started work yet) or `RUNNING` (in flight).
_NON_TERMINAL_JOB_STATUSES = frozenset(ScrapeJobStatus) - _TERMINAL_JOB_STATUSES

# `recover_stalled_batches` only ever acts on a job actually in flight —
# a `PENDING` job has no `started_at` yet, so there is nothing to stall.
_RUNNING_JOB_STATUSES = frozenset({ScrapeJobStatus.RUNNING})

# A target in one of these statuses has progressed past "never picked
# up" — `finalize_jobs` requires ALL of a job's targets to be terminal
# before finalizing; `recover_stalled_batches` requires a target to be
# in NONE of these (still bare `PENDING`) before it is eligible for
# re-dispatch.
_TERMINAL_TARGET_STATUSES = frozenset(
    {
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
    }
)


def _resolve_domains_and_modes(
    session: Session,
    workspace_id: uuid.UUID | str,
    targets: list[ScrapeJobTarget],
) -> list[ResolvedTarget]:
    """Resolve each target's `competitor_domain` + `mode`, set-based.

    One scoped read over the matches + one scoped read over the
    competitors (never a per-target query) — the scrape mode comes from
    the match's `scrape_profile_id` (defaulting to HTTP when unset, the
    same default `ScrapeProfile.mode` carries at the column level). A
    target whose match/competitor can no longer be resolved (soft ref —
    a match may be archived/deleted, `contracts/models-jobs.md`) is
    skipped rather than raising.
    """
    if not targets:
        return []

    match_ids = [target.match_id for target in targets]
    matches = {
        match.id: match
        for match in session.execute(
            scoped_select(CompetitorProductMatch, workspace_id).where(
                CompetitorProductMatch.id.in_(match_ids)
            )
        )
        .scalars()
        .all()
    }

    competitor_ids = {match.competitor_id for match in matches.values()}
    domains: dict[uuid.UUID, str] = {}
    if competitor_ids:
        domains = {
            competitor.id: competitor.domain
            for competitor in session.execute(
                scoped_select(Competitor, workspace_id).where(
                    Competitor.id.in_(competitor_ids)
                )
            )
            .scalars()
            .all()
        }

    profile_ids = {
        match.scrape_profile_id for match in matches.values() if match.scrape_profile_id is not None
    }
    modes: dict[uuid.UUID, ScrapeProfileMode] = {}
    if profile_ids:
        # `ScrapeProfile` is dual-scope (own OR global) and not registered
        # in WORKSPACE_OWNED_MODELS (app_shared.models.scrape_profiles
        # docstring) -- a plain id-lookup is the sanctioned path; the
        # profile's assignability to this workspace was already enforced
        # at match create/update time (contracts/assignment-enforcement.md).
        modes = {
            profile.id: profile.mode
            for profile in session.execute(
                select(ScrapeProfile).where(ScrapeProfile.id.in_(profile_ids))
            )
            .scalars()
            .all()
        }

    resolved: list[ResolvedTarget] = []
    for target in targets:
        match = matches.get(target.match_id)
        if match is None:
            continue
        domain = domains.get(match.competitor_id)
        if domain is None:
            continue
        mode = (
            modes.get(match.scrape_profile_id, ScrapeProfileMode.HTTP)
            if match.scrape_profile_id is not None
            else ScrapeProfileMode.HTTP
        )
        resolved.append(
            ResolvedTarget(match_id=target.match_id, competitor_domain=domain, mode=mode)
        )

    return resolved


def _scan_job_refs(
    session: Session, statuses: frozenset[ScrapeJobStatus]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Resolve `(job_id, workspace_id)` pairs for every job in `statuses`.

    A periodic maintenance sweep necessarily spans every workspace before
    it can scope each job's own subsequent read/write — mirrors the
    router precedent of resolving `(id, workspace_id)` pairs unscoped
    (e.g. `apps/api/app/routers/matches.py`), then re-scoping via
    `set_workspace_context` per row before touching anything else. This
    is the one place in the module a workspace-owned model is queried
    without a `workspace_id` predicate already in hand.
    """
    stmt = select(ScrapeJob.id, ScrapeJob.workspace_id).where(  # noqa: workspace-scope
        ScrapeJob.status.in_(statuses)
    )
    return list(session.execute(stmt).all())


@app.task(name=SCRAPE_DISPATCH_JOB)
def dispatch_job(scrape_job_id: str, workspace_id: str) -> None:
    """Expand `scrape_job_id`'s PENDING targets into domain/mode-grouped Scrapyd runs.

    Idempotent: a duplicate/at-least-once delivery re-plans the exact
    same batches (deterministic `batch_index`) and re-attempts
    `client.schedule` for each — the client's Redis `SET NX` guard on
    `dispatched:{scrape_job_id}:{batch_index}` neutralizes any repeat
    POST (FR-013, SC-003).
    """
    settings = get_settings()
    workspace_uuid = uuid.UUID(str(workspace_id))
    job_uuid = uuid.UUID(str(scrape_job_id))

    with get_session() as session:
        set_workspace_context(session, workspace_uuid)

        job = scoped_get(session, ScrapeJob, job_uuid, workspace_uuid)
        if job is None:
            return

        targets = list(
            session.execute(
                scoped_select(ScrapeJobTarget, workspace_uuid).where(
                    ScrapeJobTarget.scrape_job_id == job.id,
                    # SPEC-11 US3 (contracts/overflow-dispatch.md §4): also
                    # pick up DEFERRED targets (requeue-cap overflow handed
                    # back here for re-dispatch) alongside plain PENDING --
                    # on pickup they transition DEFERRED -> STARTED, re-
                    # entering the lock+limiter gate (FR-019). The stalled-
                    # target reaper below (`recover_stalled_batches`) is a
                    # separate query and is deliberately NOT changed here --
                    # DEFERRED must never be treated as stalled.
                    ScrapeJobTarget.status.in_(
                        (ScrapeTargetStatus.PENDING, ScrapeTargetStatus.DEFERRED)
                    ),
                )
            )
            .scalars()
            .all()
        )

        if job.status not in _TERMINAL_JOB_STATUSES and job.started_at is None:
            job.status = ScrapeJobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)

        resolved_targets = _resolve_domains_and_modes(session, workspace_uuid, targets)
        batches = plan_batches(
            resolved_targets,
            http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN,
            http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX,
        )

        client = ScrapydDispatchClient(settings=settings)
        for batch in batches:
            if batch.mode == ScrapeProfileMode.BROWSER:
                project, spider, nodes = (
                    _SCRAPYD_BROWSER_PROJECT,
                    _GENERIC_BROWSER_SPIDER,
                    settings.SCRAPYD_BROWSER_URLS,
                )
            else:
                project, spider, nodes = (
                    _SCRAPYD_PROJECT,
                    _GENERIC_PRICE_SPIDER,
                    settings.SCRAPYD_HTTP_URLS,
                )
            node_url = select_node(batch.domain, nodes)
            client.schedule(
                project,
                spider,
                workspace_id=str(workspace_uuid),
                scrape_job_id=str(job.id),
                match_ids=batch.match_ids,
                mode=batch.mode,
                batch_index=batch.batch_index,
                node_url=node_url,
            )

        session.commit()


def refresh_job_counters(
    session: Session, job: ScrapeJob, workspace_id: uuid.UUID | str
) -> Counts:
    """Overwrite `job`'s counters from `aggregate_counts` in a single UPDATE.

    Never a per-target increment (FR-018, SC-004) — `finalize_jobs` calls
    this for every non-terminal job it scans, whether or not that job's
    targets are all terminal yet, so in-flight progress counts stay
    accurate even before a job fully finalizes.
    """
    counts = aggregate_counts(session, job.id, workspace_id)
    job.success_count = counts.success
    job.failure_count = counts.failure
    job.skipped_count = counts.skipped
    return counts


def _strategy_profile_ids_for_targets(
    session: Session, workspace_id: uuid.UUID | str, targets: list[ScrapeJobTarget]
) -> list[uuid.UUID]:
    """Resolve the distinct `domain_strategy_profiles` ids this job's
    targets' matches map to (SPEC-12 US5 T036, contracts/stats-buffer.md
    §Flush, job-finalization flush trigger) -- one set-based join over the
    job's own already-loaded targets, never per-target (mirrors
    `_resolve_domains_and_modes`'s one-read-per-job shape). A match whose
    `(competitor domain, url_pattern)` key never got a profile seeded
    (e.g. discovery hasn't run yet) contributes nothing -- `flush_stats`
    is simply a no-op for that job's (empty) `profile_ids`.
    """
    if not targets:
        return []

    match_ids = [target.match_id for target in targets]
    stmt = (
        select(DomainStrategyProfile.id)
        .select_from(CompetitorProductMatch)
        .join(
            Competitor,
            (Competitor.workspace_id == CompetitorProductMatch.workspace_id)
            & (Competitor.id == CompetitorProductMatch.competitor_id),
        )
        .join(
            DomainStrategyProfile,
            (DomainStrategyProfile.workspace_id == CompetitorProductMatch.workspace_id)
            & (DomainStrategyProfile.competitor_id == CompetitorProductMatch.competitor_id)
            & (DomainStrategyProfile.domain == Competitor.domain)
            & (DomainStrategyProfile.url_pattern == CompetitorProductMatch.url_pattern),
        )
        .where(
            CompetitorProductMatch.workspace_id == workspace_id,
            CompetitorProductMatch.id.in_(match_ids),
        )
        .distinct()
    )
    return [row[0] for row in session.execute(stmt).all()]


@app.task(name=SCRAPE_FINALIZE_JOBS)
def finalize_jobs() -> None:
    """Aggregate counters and deterministically finalize non-terminal jobs.

    For every job not yet in a terminal status: `set_workspace_context`,
    refresh its counters (one UPDATE, never per-target), and — only once
    ALL of its targets have reached a terminal status — resolve
    `status = resolve_finalized_status(...)` and stamp `completed_at`.

    Idempotent: a job already terminal is skipped outright, so re-running
    this task against an already-finalized job is a no-op (FR-019).

    SPEC-12 US5 (T036, contracts/stats-buffer.md §Flush, FR-023): once a
    job actually finalizes, also enqueue `STRATEGY_STATS_FLUSH` for the
    distinct `domain_strategy_profiles` its targets' matches map to — so a
    job's buffered stats flush promptly at job end rather than waiting up
    to a full `STRATEGY_STATS_FLUSH_INTERVAL_SECONDS` for the periodic
    sweep. A job whose targets resolve no strategy profile at all (e.g.
    every match predates SPEC-12 discovery) enqueues nothing -- `flush_stats`
    is never called with an empty `profile_ids` list.
    """
    with get_session() as session:
        for job_id, workspace_id in _scan_job_refs(session, _NON_TERMINAL_JOB_STATUSES):
            set_workspace_context(session, workspace_id)

            job = scoped_get(session, ScrapeJob, job_id, workspace_id)
            if job is None or job.status in _TERMINAL_JOB_STATUSES:
                continue

            targets = list(
                session.execute(
                    scoped_select(ScrapeJobTarget, workspace_id).where(
                        ScrapeJobTarget.scrape_job_id == job.id
                    )
                )
                .scalars()
                .all()
            )

            counts = refresh_job_counters(session, job, workspace_id)

            all_terminal = all(target.status in _TERMINAL_TARGET_STATUSES for target in targets)
            if not all_terminal:
                continue

            job.status = resolve_finalized_status(
                counts.success, counts.failure, counts.skipped, counts.total
            )
            job.completed_at = datetime.now(timezone.utc)

            profile_ids = _strategy_profile_ids_for_targets(session, workspace_id, targets)
            if profile_ids:
                enqueue(
                    STRATEGY_STATS_FLUSH,
                    queue="maintenance",
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "profile_ids": [str(profile_id) for profile_id in profile_ids],
                    },
                )

        session.commit()


@app.task(name=SCRAPE_RECOVER_STALLED)
def recover_stalled_batches() -> None:
    """Re-dispatch batches whose targets never left PENDING past the stall timeout.

    Scans RUNNING jobs with `started_at` set; for each, selects targets
    still bare `PENDING` (never progressed to STARTED/terminal) and not
    `locked_at`-live, whose age past the job's `started_at` exceeds
    `SCRAPE_STALL_TIMEOUT_SECONDS`. Re-resolves each stalled target's
    domain/mode set-based (the same one-read pattern as `dispatch_job`,
    not per-target — U3), re-plans batches, and re-dispatches each to a
    deterministically selected, mode-appropriate node under a
    stall-window-bucketed `batch_index` (`:r{stall_window(...)}`) — the
    reused `SET NX` guard neutralizes a duplicate recovery delivery
    within one window; the next window mints a fresh key, permitting a
    genuine later retry if the batch is still stalled (D4, FR-015, I1).
    """
    settings = get_settings()
    timeout = settings.SCRAPE_STALL_TIMEOUT_SECONDS
    now = datetime.now(timezone.utc)
    window = stall_window(now, timeout)

    with get_session() as session:
        client = ScrapydDispatchClient(settings=settings)

        for job_id, workspace_id in _scan_job_refs(session, _RUNNING_JOB_STATUSES):
            set_workspace_context(session, workspace_id)

            job = scoped_get(session, ScrapeJob, job_id, workspace_id)
            if job is None or job.started_at is None:
                continue

            age_seconds = (now - job.started_at).total_seconds()
            if age_seconds <= timeout:
                continue

            stalled_targets = list(
                session.execute(
                    scoped_select(ScrapeJobTarget, workspace_id).where(
                        ScrapeJobTarget.scrape_job_id == job.id,
                        ScrapeJobTarget.status == ScrapeTargetStatus.PENDING,
                        ScrapeJobTarget.locked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            if not stalled_targets:
                continue

            resolved_targets = _resolve_domains_and_modes(session, workspace_id, stalled_targets)
            re_batches = plan_batches(
                resolved_targets,
                http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN,
                http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX,
            )

            for batch in re_batches:
                if batch.mode == ScrapeProfileMode.BROWSER:
                    project, spider, nodes = (
                        _SCRAPYD_BROWSER_PROJECT,
                        _GENERIC_BROWSER_SPIDER,
                        settings.SCRAPYD_BROWSER_URLS,
                    )
                else:
                    project, spider, nodes = (
                        _SCRAPYD_PROJECT,
                        _GENERIC_PRICE_SPIDER,
                        settings.SCRAPYD_HTTP_URLS,
                    )
                node_url = select_node(batch.domain, nodes)
                client.schedule(
                    project,
                    spider,
                    workspace_id=str(workspace_id),
                    scrape_job_id=str(job.id),
                    match_ids=batch.match_ids,
                    mode=batch.mode,
                    batch_index=f"{batch.batch_index}:r{window}",
                    node_url=node_url,
                )

        session.commit()
