"""`scrape_dispatch` dispatch task (`contracts/dispatch-task.md`) — SPEC-08 US1.

`dispatch_job` — the `scrape_dispatch`-queue Celery task that expands a
job into Scrapyd runs. Thin orchestrator over the pure
`app_shared.jobs.batching`/`nodes` logic + the reused SPEC-07
`ScrapydDispatchClient`. Relies on the existing `worker_process_init` ->
`dispose_engine` fork-safety hook (`celery_app.py`, FR-016) — never
starts Scrapy in-process (Principle V).

`finalize_jobs`/`refresh_job_counters`/`recover_stalled_batches`
(`maintenance` queue, US3) are added to this same module in a later
phase.
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
from app_shared.jobs.nodes import select_node
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.repository import scoped_get, scoped_select
from app_shared.scrapyd import ScrapydDispatchClient
from app_shared.task_names import SCRAPE_DISPATCH_JOB

# The Scrapy project + spider deployed to the Scrapyd nodes (apps/scrapers) —
# unchanged from the SPEC-07 thin `dispatch.generic_price_spider` task.
_SCRAPYD_PROJECT = "price_monitor"
_GENERIC_PRICE_SPIDER = "generic_price_spider"

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
                    ScrapeJobTarget.status == ScrapeTargetStatus.PENDING,
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
            nodes = (
                settings.SCRAPYD_BROWSER_URLS
                if batch.mode == ScrapeProfileMode.BROWSER
                else settings.SCRAPYD_HTTP_URLS
            )
            node_url = select_node(batch.domain, nodes)
            client.schedule(
                _SCRAPYD_PROJECT,
                _GENERIC_PRICE_SPIDER,
                workspace_id=str(workspace_uuid),
                scrape_job_id=str(job.id),
                match_ids=batch.match_ids,
                mode=batch.mode,
                batch_index=batch.batch_index,
                node_url=node_url,
            )

        session.commit()
