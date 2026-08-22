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

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.database import get_session, get_system_session, set_workspace_context
from app_shared.enums import ScrapeJobStatus, ScrapeProfileMode, ScrapeTargetStatus
from app_shared.ids import new_uuid7
from app_shared.jobs.batching import ResolvedTarget, plan_batches
from app_shared.jobs.lifecycle import resolve_finalized_status, stall_window
from app_shared.jobs.nodes import select_node
from app_shared.jobs.targets import Counts, aggregate_counts
from app_shared.messaging import enqueue
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.outbox import write_outbox_message
from app_shared.repository import scoped_get, scoped_select
from app_shared.scrapyd import ScrapydDispatchClient
from app_shared.task_names import (
    CREATE_WEBHOOK_EVENT,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_REDISPATCH_JOBS,
    STRATEGY_STATS_FLUSH,
)
from app_shared.webhooks.payloads import build_job_event

logger = logging.getLogger(__name__)

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

# Distinguishes "this node has not been probed yet" from "this node was
# probed and came back unreachable" (a real, cached `None`) — F-2's node
# liveness cache must never re-probe a node it already found dead.
_UNPROBED = object()

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


def _scan_job_refs(statuses: frozenset[ScrapeJobStatus]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Resolve `(job_id, workspace_id)` pairs for every job in `statuses`.

    A periodic maintenance sweep necessarily spans every workspace, and
    under FORCE ROW LEVEL SECURITY the ordinary engine's role fail-closes
    an unscoped scan to ZERO rows when no workspace context is set — which
    silently killed finalization for 6.5 h on 2026-08-21 (mushtryati F-1).
    So the id-pair scan runs on the sanctioned BYPASSRLS system session
    (`get_system_session`, the outbox-dispatcher / scheduler-claim
    precedent) and returns plain ids; EVERY subsequent row read/write
    happens on the caller's ordinary session, re-scoped per job via
    `set_workspace_context` — the system session never touches a row.
    """
    with get_system_session() as session:
        stmt = select(ScrapeJob.id, ScrapeJob.workspace_id).where(  # noqa: workspace-scope
            ScrapeJob.status.in_(statuses)
        )
        return list(session.execute(stmt).all())


@app.task(name=SCRAPE_DISPATCH_JOB)
def dispatch_job(scrape_job_id: str, workspace_id: str) -> None:
    """Expand `scrape_job_id`'s PENDING targets into domain/mode-grouped Scrapyd runs.

    Idempotent per TARGET, not merely per Redis guard window (F-2,
    2026-08-22): selection is `(PENDING AND dispatched_at IS NULL) OR
    DEFERRED`, and every target carried in a POSTed batch is stamped
    `dispatched_at` before the commit. A duplicate/at-least-once delivery
    therefore re-plans nothing it already sent. The client's Redis
    `SET NX` guard on `dispatched:{scrape_job_id}:{batch_index}` still
    neutralizes a repeat POST inside one TTL window (FR-013, SC-003), but
    it is no longer what carries the guarantee — its TTL (900s) is far
    shorter than scrapyd queue latency, which is why the 2026-08-21
    mushtryati run re-POSTed the whole backlog at every guard expiry
    (11,830 attempts over 4,372 targets = 2.71x, ~$0.50 wasted).

    The contract this establishes for the rest of the pipeline: a PENDING
    target with `dispatched_at IS NOT NULL` is scrapyd's problem (or, past
    `SCRAPE_STALL_TIMEOUT_SECONDS`, `recover_stalled_batches`'s), never
    this task's.
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
                    #
                    # F-2 (2026-08-22): only never-dispatched PENDING plus
                    # DEFERRED handbacks. A PENDING target with a
                    # dispatched_at stamp is queued on a scrapyd node (or
                    # the reaper's problem past the stall timeout) -- re-
                    # planning it here is what re-POSTed the entire backlog
                    # on every guard expiry during the 2026-08-21 run.
                    or_(
                        and_(
                            ScrapeJobTarget.status == ScrapeTargetStatus.PENDING,
                            ScrapeJobTarget.dispatched_at.is_(None),
                        ),
                        ScrapeJobTarget.status == ScrapeTargetStatus.DEFERRED,
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
            browser_max=settings.SCRAPE_BATCH_BROWSER_MAX,
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
            # F-2 (2026-08-22): stamp the batch's targets the moment they
            # leave here, so the next dispatch delivery cannot re-plan
            # them. A guard-deduped "already scheduled" return counts as
            # dispatched too -- the POST that guard is standing in for did
            # happen. One loop over the already-loaded `targets`, never an
            # extra query.
            dispatched_match_ids = set(batch.match_ids)
            stamp = datetime.now(timezone.utc)
            for target in targets:
                if target.match_id in dispatched_match_ids:
                    target.dispatched_at = stamp

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
    job actually finalizes, also request `STRATEGY_STATS_FLUSH` for the
    distinct `domain_strategy_profiles` its targets' matches map to — so a
    job's buffered stats flush promptly at job end rather than waiting up
    to a full `STRATEGY_STATS_FLUSH_INTERVAL_SECONDS` for the periodic
    sweep. A job whose targets resolve no strategy profile at all (e.g.
    every match predates SPEC-12 discovery) requests nothing -- `flush_stats`
    is never called with an empty `profile_ids` list.

    SPEC-16 US3 (T034, contracts/events.md #2): once a job actually
    finalizes, one `create_webhook_event` is requested per finalized job
    via `build_job_event` — `CANCELLED` (never produced by this path) and
    any non-terminal status emit nothing.

    2026-08-15 audit risk H1: BOTH follow-ups above are now written to the
    transactional outbox (`app_shared.outbox.write_outbox_message`) inside
    the same transaction as the finalize, instead of being sent to the
    broker around it. That fixes two distinct defects at once: the stats
    flush used to be enqueued *before* `session.commit()` (a rollback left
    a flush chasing a job that never finalized), and the webhook event was
    enqueued *after* it with the broker error swallowed (a Redis outage
    silently dropped the terminal-status event of a genuinely finished
    job). Neither can happen now — the messages commit with the finalize
    or not at all, and the outbox dispatcher publishes them with
    at-least-once delivery and bounded retries.
    """
    with get_session() as session:
        for job_id, workspace_id in _scan_job_refs(_NON_TERMINAL_JOB_STATUSES):
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

            # Audit H1: this was a *pre-commit* `enqueue` — it fired the
            # stats flush before the finalize it depends on had
            # committed, so a rollback below left a flush racing (or
            # preceding) a job that never finalized. Written to the
            # outbox instead, it now commits atomically with the
            # finalize and is published afterwards.
            profile_ids = _strategy_profile_ids_for_targets(session, workspace_id, targets)
            if profile_ids:
                write_outbox_message(
                    session,
                    workspace_id=workspace_id,
                    task_name=STRATEGY_STATS_FLUSH,
                    queue="maintenance",
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "profile_ids": [str(profile_id) for profile_id in profile_ids],
                    },
                    dedup_key=f"statsflush:{job.id}",
                    now=job.completed_at,
                )

            # SPEC-16 US3 (T034, contracts/events.md #2), reworked for
            # audit H1: the job event was a post-commit fire-and-forget
            # enqueue whose failure was swallowed, so a broker outage
            # silently dropped the terminal-status event of a job that
            # had genuinely finished. It is now an outbox row written in
            # the same transaction as the finalize. `CANCELLED` (never
            # produced by this path) and any non-terminal status still
            # emit nothing (`build_job_event` returns `None`).
            built = build_job_event(
                scrape_job_id=job.id,
                status=job.status,
                success_count=counts.success,
                failure_count=counts.failure,
                skipped_count=counts.skipped,
                total=counts.total,
            )
            if built is not None:
                webhook_event_type, webhook_payload, dedup_key = built
                # The message id doubles as the consumer's idempotency
                # key -- see `create_webhook_event`.
                message_id = new_uuid7()
                write_outbox_message(
                    session,
                    workspace_id=workspace_id,
                    task_name=CREATE_WEBHOOK_EVENT,
                    queue="webhook_events",
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "event_type": webhook_event_type,
                        "payload": webhook_payload,
                        "dedup_key": dedup_key,
                        "event_id": str(message_id),
                        "occurred_at": job.completed_at.isoformat(),
                    },
                    dedup_key=dedup_key,
                    now=job.completed_at,
                    message_id=message_id,
                )

        session.commit()


@app.task(name=SCRAPE_REDISPATCH_JOBS)
def redispatch_pending_jobs() -> None:
    """Re-enqueue `dispatch_job` for jobs whose targets nothing will pick up.

    Closes the DEFERRED deadlock (PLAN_AMAZON_NOON_PRICING Phase 1):
    `dispatch_job` selects PENDING **and** DEFERRED targets, but nothing
    ever re-enqueued it — a target handed back as DEFERRED by the
    requeue-cap overflow sat forever, wedging any run past ~20 products.
    Two cases per non-terminal job, chosen to not overlap
    `recover_stalled_batches` (which owns stalled bare-PENDING targets on
    RUNNING jobs):

    - job still `PENDING` with `started_at IS NULL` — its original
      dispatch delivery was lost; re-enqueue unconditionally.
    - job has >= 1 `DEFERRED` target — re-enqueue so `dispatch_job`
      re-plans them; on pickup they re-enter the lock+limiter gate.
    - job has >= 1 `PENDING` target with `dispatched_at IS NULL` (F-2,
      2026-08-22) — a dispatch delivery lost *after* the job started. Now
      that `dispatch_job` refuses to re-plan an already-stamped target,
      an unstamped one is the only shape nothing else would pick up:
      `recover_stalled_batches` owns the stamped-but-unprogressed ones.

    Duplicate-delivery safety is unchanged: the dispatch client's Redis
    `SET NX` guard (now TTL-bounded, `SCRAPYD_DISPATCH_GUARD_TTL_SECONDS`)
    still deduplicates re-POSTs of the same `(job, batch_index)` within
    the TTL window — which also paces how often a still-deferred batch
    can actually re-POST. Idempotent and fire-and-forget: a broker error
    on one job is logged and the sweep moves on.
    """
    with get_session() as session:
        for job_id, workspace_id in _scan_job_refs(_NON_TERMINAL_JOB_STATUSES):
            set_workspace_context(session, workspace_id)

            job = scoped_get(session, ScrapeJob, job_id, workspace_id)
            if job is None or job.status in _TERMINAL_JOB_STATUSES:
                continue

            needs_redispatch = job.started_at is None
            if not needs_redispatch:
                deferred_exists = (
                    session.execute(
                        scoped_select(ScrapeJobTarget, workspace_id)
                        .where(
                            ScrapeJobTarget.scrape_job_id == job.id,
                            ScrapeJobTarget.status == ScrapeTargetStatus.DEFERRED,
                        )
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                needs_redispatch = deferred_exists is not None
            if not needs_redispatch:
                # F-2 (2026-08-22): a dispatch delivery lost AFTER the job
                # started leaves PENDING targets that were never POSTed --
                # `started_at IS NULL` no longer catches them and
                # `recover_stalled_batches` owns only targets that WERE
                # dispatched, so without this probe nothing ever re-enqueues
                # them and the job wedges short of its target count.
                undispatched_pending = (
                    session.execute(
                        scoped_select(ScrapeJobTarget, workspace_id)
                        .where(
                            ScrapeJobTarget.scrape_job_id == job.id,
                            ScrapeJobTarget.status == ScrapeTargetStatus.PENDING,
                            ScrapeJobTarget.dispatched_at.is_(None),
                        )
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                needs_redispatch = undispatched_pending is not None
            if not needs_redispatch:
                continue

            try:
                enqueue(
                    SCRAPE_DISPATCH_JOB,
                    queue="scrape_dispatch",
                    kwargs={
                        "scrape_job_id": str(job.id),
                        "workspace_id": str(workspace_id),
                    },
                )
                logger.info(
                    "redispatch_pending_jobs: re-enqueued dispatch for job %s "
                    "(started_at=%s)",
                    job.id,
                    job.started_at,
                )
            except Exception:
                logger.exception(
                    "redispatch_pending_jobs: failed to re-enqueue job %s", job.id
                )


@app.task(name=SCRAPE_RECOVER_STALLED)
def recover_stalled_batches() -> None:
    """Re-dispatch batches whose targets never left PENDING past the stall timeout.

    Scans RUNNING jobs with `started_at` set; for each, selects targets
    still bare `PENDING` (never progressed to STARTED/terminal), not
    `locked_at`-live, and whose OWN `dispatched_at` is older than
    `SCRAPE_STALL_TIMEOUT_SECONDS` (F-2, 2026-08-22 — see the query
    below for why job-age was wrong). Before re-POSTing, the target
    scrapyd node is probed: a node alive and working its queue means
    those targets are queued, not stalled. Re-resolves each stalled
    target's
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

    # One liveness probe per node per task invocation — N batches landing
    # on the same node must not become N `daemonstatus.json` round-trips.
    node_status_cache: dict = {}

    with get_session() as session:
        client = ScrapydDispatchClient(settings=settings)

        for job_id, workspace_id in _scan_job_refs(_RUNNING_JOB_STATUSES):
            set_workspace_context(session, workspace_id)

            job = scoped_get(session, ScrapeJob, job_id, workspace_id)
            if job is None or job.started_at is None:
                continue

            # F-2 (2026-08-22): age per TARGET (its own last dispatch), not
            # per job — job-age classified every rate-limited tail target
            # as stalled from minute 15 onward on 2026-08-21.
            cutoff = now - timedelta(seconds=timeout)
            stalled_targets = list(
                session.execute(
                    scoped_select(ScrapeJobTarget, workspace_id).where(
                        ScrapeJobTarget.scrape_job_id == job.id,
                        ScrapeJobTarget.status == ScrapeTargetStatus.PENDING,
                        ScrapeJobTarget.locked_at.is_(None),
                        ScrapeJobTarget.dispatched_at.is_not(None),
                        ScrapeJobTarget.dispatched_at < cutoff,
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
                browser_max=settings.SCRAPE_BATCH_BROWSER_MAX,
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
                status_payload = node_status_cache.get(node_url, _UNPROBED)
                if status_payload is _UNPROBED:
                    status_payload = client.daemon_status(node_url)
                    node_status_cache[node_url] = status_payload
                if status_payload is not None and (
                    int(status_payload.get("pending", 0))
                    + int(status_payload.get("running", 0))
                ) > 0:
                    # Node alive and working its queue: these targets are
                    # queued behind max_proc/rate limits, not stalled.
                    continue
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
                # A re-POSTed target's stall clock restarts here — without
                # a fresh stamp the very next sweep would reap it again,
                # which is the feedback loop this whole fix removes.
                stamp = datetime.now(timezone.utc)
                batch_match_ids = set(batch.match_ids)
                for target in stalled_targets:
                    if target.match_id in batch_match_ids:
                        target.dispatched_at = stamp

        session.commit()
