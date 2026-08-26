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
from app.workers.tasks_dispatch import DispatchedBatch, stamp_targets_dispatched
from app_shared.config import get_settings
from app_shared.costauth import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_PROXY,
    AuthorizationPurpose,
    AuthorizationRequest,
    CostAuthorizationService,
    authorize_or_none,
    estimate_bytes,
    estimate_cost_minor_units,
)
from app_shared.database import get_session, get_system_session, set_workspace_context
from app_shared.domains.lifecycle import unsupported_target_outcome
from app_shared.domains.state_lookup import get_domain_state
from app_shared.enums import ScrapeJobStatus, ScrapeProfileMode, ScrapeTargetStatus
from app_shared.ids import new_uuid7
from app_shared.jobs.batching import (
    DEFAULT_STRATEGY_METHOD,
    Batch,
    ResolvedTarget,
    plan_batches,
)
from app_shared.jobs.dispatch_intents import DispatchIntentStore
from app_shared.jobs.lifecycle import resolve_finalized_status, stall_window
from app_shared.jobs.reconciliation import reconcile_successful_failed_targets
from app_shared.jobs.nodes import select_node
from app_shared.jobs.targets import Counts, aggregate_counts, mark_target
from app_shared.messaging import enqueue
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_task
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.domain_playbooks import DomainState
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyMethod, DomainStrategyProfile
from app_shared.outbox import write_outbox_message
from app_shared.repository import scoped_get, scoped_select
from app_shared.strategy.methods import mode_for_access_method, resolve_method_candidate
from app_shared.scrapyd import (
    DispatchIdentity,
    ScrapydDispatchClient,
    build_dispatch_identity,
)
from app_shared.task_names import (
    CREATE_WEBHOOK_EVENT,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_RECONCILE_FALSE_FAILURES,
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


@maintenance_task(scope=MaintenanceScope.WORKSPACE)
@app.task(name=SCRAPE_RECONCILE_FALSE_FAILURES)
def reconcile_false_failed_targets(
    workspace_id: str,
    scrape_job_id: str,
    *,
    dry_run: bool = True,
    expected_match_ids: list[str] | None = None,
    requested_by: str | None = None,
) -> dict[str, object]:
    """Preview/apply the bounded false-failure lifecycle repair.

    Dry-run is the safe default.  Applying requires the exact match-id list
    returned by a reviewed dry-run; the shared service locks/rechecks the set
    before changing any row.  The structured log records who requested the
    operation and its complete report even though this deployment does not
    configure a Celery result backend.
    """

    if not dry_run and not requested_by:
        raise ValueError("requested_by is required for an applying reconciliation")

    workspace_uuid = uuid.UUID(str(workspace_id))
    job_uuid = uuid.UUID(str(scrape_job_id))
    with get_session() as session:
        set_workspace_context(session, workspace_uuid)
        report = reconcile_successful_failed_targets(
            session,
            workspace_id=workspace_uuid,
            scrape_job_id=job_uuid,
            dry_run=dry_run,
            expected_match_ids=expected_match_ids,
        )
        if not dry_run:
            session.commit()
    payload = report.as_dict()
    logger.info(
        "scrape_target_reconciliation requested_by=%s report=%s",
        requested_by,
        payload,
    )
    return payload


def _queue_depth(status_payload: dict) -> int | None:
    """`pending + running` off a `daemonstatus.json` payload, or `None`.

    F-7 (2026-08-22 review): `ScrapydDispatchClient.daemon_status`
    deliberately never raises — "a probe that blew up would abort the
    whole sweep on the very node it exists to classify" — so the caller
    must not reintroduce the raise by coercing whatever the node put in
    the JSON. Mirrors `app_shared.opsmetrics.snapshot._as_int`: a
    malformed payload (`"pending": null`, a list, a non-numeric string)
    is not evidence that the node is working a queue, so it is treated
    exactly like an unreachable node — `None`, and the reap proceeds.
    """
    try:
        return int(status_payload.get("pending", 0)) + int(
            status_payload.get("running", 0)
        )
    except (TypeError, ValueError):
        return None


# A target in one of these statuses has progressed past "never picked
# up" — `finalize_jobs` requires ALL of a job's targets to be terminal
# before finalizing; `recover_stalled_batches` requires a target to be
# in NONE of these (still bare `PENDING`) before it is eligible for
# re-dispatch.
#
# `CANCELLED` (EPA A2) is a member: an administratively cancelled target
# is finished. Without it `finalize_jobs` would wait forever for targets
# nothing will ever pick up, and `recover_stalled_batches` would keep
# re-dispatching a job a human had explicitly closed.
_TERMINAL_TARGET_STATUSES = frozenset(
    {
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
        ScrapeTargetStatus.CANCELLED,
    }
)


def _batch_route(batch: Batch, settings) -> tuple[str, str, list[str]]:
    """The `(project, spider, node pool)` one batch is bound for.

    Extracted from the two dispatch loops so the *planning* pass and the
    *POST* pass cannot drift: the batch's node class is part of its
    dispatch identity, so deriving it twice from two copies of this
    branch would be a way to mint two identities for one batch.
    """
    if batch.mode == ScrapeProfileMode.BROWSER:
        return (
            _SCRAPYD_BROWSER_PROJECT,
            _GENERIC_BROWSER_SPIDER,
            settings.SCRAPYD_BROWSER_URLS,
        )
    return _SCRAPYD_PROJECT, _GENERIC_PRICE_SPIDER, settings.SCRAPYD_HTTP_URLS


def _batch_authorization_request(
    batch: Batch,
    *,
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    purpose: AuthorizationPurpose,
    identity: DispatchIdentity,
) -> AuthorizationRequest:
    """The C3 authorization one batch needs, derived from the plan (EPA C3).

    Two things the planner genuinely knows are used, and nothing is
    invented beyond them:

    * **transport/provider** come from the batch's MODE. A ``BROWSER``
      batch is a browser navigation (the expensive path, and the only one
      that costs browser-seconds); everything else is authorized as
      ``PROXY``. Authorizing an HTTP batch as paid even though its
      strategy chain may resolve to a DIRECT step is the FAIL-CLOSED
      choice: the planner cannot know which rung the spider will land on,
      and over-reserving is corrected at settlement while under-reserving
      is money spent outside any ceiling.
    * **size** comes from the batch's own match count, priced with the
      MEASURED per-domain rate (``app_shared.opsmetrics.cost``), not a
      guess.

    ``dedupe_key`` is the batch's dispatch identity key (EPA B1). That is
    exactly the right grain: a duplicate/at-least-once delivery of the
    same batch re-derives the same identity, so it collapses onto the
    grant it already holds instead of reserving the budget twice — the
    same property, on the money side, that the identity already gives the
    POST side.
    """
    is_browser = batch.mode == ScrapeProfileMode.BROWSER
    requests = max(1, len(batch.match_ids))
    return AuthorizationRequest(
        workspace_id=workspace_id,
        domain=batch.domain,
        transport="BROWSER" if is_browser else "PROXY",
        provider=FLEET_PROVIDER_BROWSER if is_browser else FLEET_PROVIDER_PROXY,
        estimated_bytes=estimate_bytes(requests),
        estimated_cost_minor_units=estimate_cost_minor_units(batch.domain, requests),
        purpose=purpose,
        estimated_requests=requests,
        estimated_browser_seconds=requests * 30 if is_browser else 0,
        scrape_job_id=scrape_job_id,
        dedupe_key=identity.key,
    )


def _skip_unsupported_batch(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    batch: Batch,
) -> bool:
    """EPA W4.1: never dispatch a target whose domain has been certified
    ``DomainState.UNSUPPORTED`` -- and never let it look like something
    worth retrying.

    ``unsupported_target_outcome()`` (``app_shared.domains.lifecycle``)
    is the product-visible, zero-retry outcome contract for this state:
    it names the exact ``(status, error_code)`` pair --
    ``SKIPPED``/``BLOCKED`` -- for ``mark_target`` (``app_shared.jobs
    .targets``, the single writer of ``scrape_job_targets``). ``SKIPPED``
    is TERMINAL, so ``mark_target``'s own "terminal is terminal"
    (2026-08-03) rule is what makes "do not retry" true here for free --
    no separate suppression bookkeeping is needed, and a later sweep
    (``redispatch_pending_jobs``/``recover_stalled_batches``) will never
    re-offer a target already in a terminal status.

    Checked BEFORE C3's ``authorize_or_none`` -- cheaper (no reservation
    spent on work that will never dispatch), and it composes rather than
    duplicates: C3 already denies ``UNSUPPORTED`` at ``authorize()``
    under its own ``DOMAIN_QUARANTINED``-family reasons (a *batch-level*
    denial with no product-visible per-target outcome), so a batch this
    function turns away here never even reaches that gate, and one that
    somehow did would still be refused by it.
    """
    if get_domain_state(session, batch.domain) is not DomainState.UNSUPPORTED:
        return False
    status, error_code = unsupported_target_outcome()
    for match_id in batch.match_ids:
        mark_target(
            session,
            workspace_id=workspace_id,
            scrape_job_id=scrape_job_id,
            match_id=match_id,
            status=status,
            error_code=error_code,
        )
    logger.info(
        "dispatch: domain=%s is UNSUPPORTED -- skipping %d target(s) instead of "
        "dispatching workspace_id=%s scrape_job_id=%s",
        batch.domain,
        len(batch.match_ids),
        workspace_id,
        scrape_job_id,
    )
    return True


def _batch_identity(
    scrape_job_id: uuid.UUID, batch: Batch, project: str, spider: str
) -> DispatchIdentity:
    """Name one batch canonically (EPA B1).

    `node_class` is the `{project}:{spider}` **pool**, deliberately not
    the selected node URL: `select_node` re-maps domains whenever the
    pool's size changes, and a pool resize must not mint a new identity
    for work already POSTed.
    """
    return build_dispatch_identity(
        scrape_job_id=str(scrape_job_id),
        planning_generation=batch.planning_generation,
        strategy_method=batch.strategy_method,
        domain=batch.domain,
        mode=batch.mode,
        node_class=f"{project}:{spider}",
        match_ids=batch.match_ids,
    )


def _strategy_method_label(method: DomainStrategyMethod | None) -> str:
    """A stable text name for one versioned strategy-chain rung (EPA B1).

    Part of the dispatch identity, so it must be **stable across
    processes and deliveries** and must change when — and only when — the
    work genuinely changes rung. The method's UUID would be stable but
    unreadable in a `dispatch_intents` row an operator is trying to
    understand; the access/extraction/version triple is both.
    """
    if method is None:
        return DEFAULT_STRATEGY_METHOD
    access = getattr(method.access_method, "value", method.access_method)
    extraction = getattr(method.extraction_method, "value", method.extraction_method)
    return f"{access}/{extraction or '-'}/v{method.method_version}"


def _resolve_domains_and_modes(
    session: Session,
    workspace_id: uuid.UUID | str,
    targets: list[ScrapeJobTarget],
) -> tuple[list[ResolvedTarget], bool]:
    """Resolve each target's `competitor_domain` + `mode` + strategy rung, set-based.

    One scoped read over the matches + one scoped read over the
    competitors (never a per-target query) — the scrape mode comes from
    the match's `scrape_profile_id` (defaulting to HTTP when unset, the
    same default `ScrapeProfile.mode` carries at the column level). A
    target whose match/competitor can no longer be resolved (soft ref —
    a match may be archived/deleted, `contracts/models-jobs.md`) is
    skipped rather than raising.

    Returns `(resolved_targets, cursor_advanced)`. `cursor_advanced` is
    True when this pass moved at least one target's durable strategy
    cursor — i.e. when the caller is about to commit a genuinely new
    plan. That flag, and nothing else, is what authorizes advancing
    `scrape_jobs.planning_generation` (EPA B1): a task retry that
    re-resolves identical cursors reports False and therefore reuses the
    persisted generation, producing the same dispatch identity and one
    POST rather than two.
    """
    if not targets:
        return [], False

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

    # Resolve durable strategy cursors set-based.  Existing handoffs retain
    # their selected method; a fresh target selects the preferred/first
    # runnable method and persists that cursor before it is scheduled.
    current_method_ids = {
        target.current_strategy_method_id
        for target in targets
        if target.current_strategy_method_id is not None
    }
    current_methods: dict[uuid.UUID, DomainStrategyMethod] = {}
    if current_method_ids:
        current_methods = {
            method.id: method
            for method in session.execute(
                scoped_select(DomainStrategyMethod, workspace_id).where(
                    DomainStrategyMethod.id.in_(current_method_ids)
                )
            )
            .scalars()
            .all()
        }

    strategy_profiles = list(
        session.execute(
            scoped_select(DomainStrategyProfile, workspace_id).where(
                DomainStrategyProfile.competitor_id.in_(competitor_ids)
            )
        )
        .scalars()
        .all()
    )
    strategy_profile_by_key = {
        (profile.competitor_id, profile.domain, profile.url_pattern): profile
        for profile in strategy_profiles
    }
    strategy_profile_ids = {profile.id for profile in strategy_profiles}
    methods_by_profile: dict[uuid.UUID, list[DomainStrategyMethod]] = {}
    if strategy_profile_ids:
        for method in (
            session.execute(
                scoped_select(DomainStrategyMethod, workspace_id).where(
                    DomainStrategyMethod.domain_strategy_profile_id.in_(strategy_profile_ids)
                )
            )
            .scalars()
            .all()
        ):
            methods_by_profile.setdefault(method.domain_strategy_profile_id, []).append(method)

    resolved: list[ResolvedTarget] = []
    cursor_advanced = False
    for target in targets:
        match = matches.get(target.match_id)
        if match is None:
            continue
        domain = domains.get(match.competitor_id)
        if domain is None:
            continue
        selected_method = current_methods.get(target.current_strategy_method_id)
        if selected_method is None:
            # Domain-scoped strategy profiles are the current default; an
            # exact URL-pattern profile remains a supported, more-specific
            # fallback for workspaces that opt into pattern scope.
            strategy_profile = strategy_profile_by_key.get(
                (match.competitor_id, domain, domain)
            ) or strategy_profile_by_key.get(
                (match.competitor_id, domain, match.url_pattern)
            )
            if strategy_profile is not None:
                selection = resolve_method_candidate(
                    methods_by_profile.get(strategy_profile.id, ()),
                    preferred_method_id=strategy_profile.preferred_method_id,
                    current_attempt_ordinal=target.strategy_attempt_ordinal,
                )
                if selection is not None:
                    selected_method = selection.method  # type: ignore[assignment]
                    target.current_strategy_method_id = selected_method.id
                    target.strategy_attempt_ordinal = selection.attempt_ordinal
                    target.chain_token = target.chain_token or new_uuid7()
                    # A durable, explicit strategy-chain transition — the
                    # ONE thing that authorizes a new planning generation
                    # (EPA B1). Recorded here rather than inferred from
                    # `session.dirty` later so the reason is visible at
                    # the point the cursor actually moves.
                    cursor_advanced = True

        if selected_method is not None:
            mode = mode_for_access_method(selected_method.access_method)
        else:
            mode = (
                modes.get(match.scrape_profile_id, ScrapeProfileMode.HTTP)
                if match.scrape_profile_id is not None
                else ScrapeProfileMode.HTTP
            )
        resolved.append(
            ResolvedTarget(
                match_id=target.match_id,
                competitor_domain=domain,
                mode=mode,
                strategy_method=_strategy_method_label(selected_method),
            )
        )

    return resolved, cursor_advanced


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


@maintenance_task(scope=MaintenanceScope.WORKSPACE)
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

        resolved_targets, cursor_advanced = _resolve_domains_and_modes(
            session, workspace_uuid, targets
        )

        # --- the durable planning generation (EPA B1) -------------------------
        # Read from `scrape_jobs.planning_generation`, advanced ONLY when
        # this pass moved a strategy cursor -- i.e. when we are about to
        # commit a genuinely new plan. A duplicate delivery, or a retry
        # after a rollback, re-derives the SAME number and therefore the
        # same dispatch identity, which is what turns an at-least-once
        # delivery into exactly one POST. The write joins the very
        # transaction that persists the cursor advance and the intents
        # below, so the generation can never outlive the plan it names.
        planning_generation = int(job.planning_generation or 0)
        if cursor_advanced:
            planning_generation += 1
            job.planning_generation = planning_generation

        batches = plan_batches(
            resolved_targets,
            http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN,
            http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX,
            browser_max=settings.SCRAPE_BATCH_BROWSER_MAX,
            planning_generation=planning_generation,
        )

        # --- persist the intents, in THIS transaction ------------------------
        # Before a single POST: the plan is durable, or it did not happen.
        # Doing it as its own pass (rather than inline in the dispatch
        # loop) is what makes "the intent row and the strategy cursor
        # commit together" true even if the first POST blows up.
        intents = DispatchIntentStore(
            session,
            workspace_id=workspace_uuid,
            scrape_job_id=job.id,
            authorized_cancellation_generation=job.cancellation_generation or 0,
        )
        planned: list[tuple[Batch, DispatchIdentity, str, str, str]] = []
        for batch in batches:
            project, spider, nodes = _batch_route(batch, settings)
            node_url = select_node(batch.domain, nodes)
            identity = _batch_identity(job.id, batch, project, spider)
            intents.plan(
                identity, match_ids=batch.match_ids, batch_index=batch.batch_index
            )
            planned.append((batch, identity, project, spider, node_url))

        client = ScrapydDispatchClient(settings=settings, intents=intents)
        # EPA C3: the cost-authorization gate. One service per task
        # invocation; it opens its own short transaction per grant, which
        # is deliberate -- the reservation must be DURABLE before the POST
        # for the same reason the dispatch intent is, and joining this
        # task's long transaction would tie every workspace's budget row
        # to the lifetime of one job's dispatch loop.
        costauth = CostAuthorizationService(default_workspace_id=workspace_uuid)
        try:
            for batch, identity, project, spider, node_url in planned:
                # EPA W4.1: a domain certified UNSUPPORTED is a
                # product-visible, zero-retry skip -- checked before C3's
                # gate (see `_skip_unsupported_batch`'s docstring for why
                # the ordering is cheaper and still composes with C3's own
                # batch-level UNSUPPORTED denial).
                if _skip_unsupported_batch(
                    session,
                    workspace_id=workspace_uuid,
                    scrape_job_id=job.id,
                    batch=batch,
                ):
                    continue
                # Paid dispatch site 1 (REFRESH) and site 2
                # (BROWSER_ESCALATION) -- the same loop, distinguished by
                # the batch's mode, because a browser batch is exactly the
                # expensive escalation C2's DEGRADED rule denies and an
                # HTTP batch is not.
                purpose = (
                    AuthorizationPurpose.BROWSER_ESCALATION
                    if batch.mode == ScrapeProfileMode.BROWSER
                    else AuthorizationPurpose.REFRESH
                )
                grant = authorize_or_none(
                    costauth,
                    _batch_authorization_request(
                        batch,
                        workspace_id=workspace_uuid,
                        scrape_job_id=job.id,
                        purpose=purpose,
                        identity=identity,
                    ),
                    site="tasks_jobs.dispatch_job",
                )
                if grant is None:
                    # Denied: do NOT POST and do NOT stamp. The targets
                    # stay PENDING/unstamped, so `redispatch_pending_jobs`
                    # will offer them again once whatever denied them
                    # (budget, breaker, entitlement, domain state) clears.
                    # A denial must never look like a dispatch.
                    continue
                try:
                    client.schedule(
                        project,
                        spider,
                        workspace_id=str(workspace_uuid),
                        scrape_job_id=str(job.id),
                        match_ids=batch.match_ids,
                        mode=batch.mode,
                        # Spider argument + traceability label ONLY -- the
                        # idempotency decision is `identity`'s (EPA B1).
                        batch_index=batch.batch_index,
                        node_url=node_url,
                        identity=identity,
                        # EPA C4b: stamp this batch's C3 grant onto the
                        # spider so its own network-ledger boundary can
                        # trace every physical operation back to it.
                        authorization_id=grant.authorization_id,
                        # EPA Phase C F3: and WHAT that grant decided, so
                        # C1's three decision columns stop being NULL.
                        budget_decision_version=grant.budget_decision_version,
                        entitlement_version=grant.entitlement_version,
                        breaker_decision=grant.breaker_decision,
                    )
                except Exception:
                    # Failure BEFORE dispatch: nothing was spent, so the
                    # whole hold goes back immediately rather than waiting
                    # out its lease. `release` is CAS-idempotent, so a
                    # retry of this task cannot double-credit the budget.
                    costauth.release(grant.authorization_id)
                    raise
                # F-2 (2026-08-22): stamp the batch's targets the moment they
                # leave here, so the next dispatch delivery cannot re-plan
                # them. A guard-deduped "already scheduled" return counts as
                # dispatched too -- the POST that guard is standing in for did
                # happen. One loop over the already-loaded `targets`, never an
                # extra query.
                #
                # EPA B2: `stamp_targets_dispatched` is the SINGLE stamping
                # path -- it re-derives proof from the same committed
                # guard/intent `client.schedule()` just confirmed (or
                # deduped against) before writing `dispatched_at` /
                # `dispatch_intent_id`, so a target can never be marked
                # dispatched without something proving THIS identity
                # reached Scrapyd. `client._redis` is the exact Redis
                # client `schedule()` just wrote the guard through --
                # reusing it (rather than building a second one) is what
                # makes the guard visible here in the same call.
                dispatched_match_ids = set(batch.match_ids)
                stamp_targets_dispatched(
                    session,
                    client._redis,  # noqa: SLF001 - the same client just POSTed through
                    batch=DispatchedBatch(
                        workspace_id=workspace_uuid,
                        scrape_job_id=job.id,
                        identity=identity,
                        targets=[
                            target
                            for target in targets
                            if target.match_id in dispatched_match_ids
                        ],
                    ),
                    stamp=datetime.now(timezone.utc),
                )
        except Exception:
            # F-1 (2026-08-22 review): a stamp is only worth what it
            # survives. `get_session()` never commits in its `finally`, so
            # with one commit after the whole loop, batch 50's unreachable
            # node used to roll back the stamps of batches 1-49 that really
            # WERE POSTed -- and once the 900s Redis guard expired, the
            # next dispatch delivery re-planned every one of them. That is
            # the exact 2.71x mechanism this phase exists to remove. Commit
            # what was earned, then let the failure propagate unchanged.
            session.commit()
            raise

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


@maintenance_task(scope=MaintenanceScope.FLEET)
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


@maintenance_task(scope=MaintenanceScope.FLEET)
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


@maintenance_task(scope=MaintenanceScope.FLEET)
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

            resolved_targets, _ = _resolve_domains_and_modes(
                session, workspace_id, stalled_targets
            )

            # A stall re-plan IS a durable, explicit replan transition, so
            # it advances the job's planning generation (EPA B1) — that is
            # what makes the re-POST a genuinely new dispatch identity
            # instead of one suppressed by the original batch's guard.
            #
            # It advances once per *pass that found stalled targets*, and
            # every re-POSTed target's `dispatched_at` is re-stamped
            # below, so a target cannot be found stalled again until a
            # further `SCRAPE_STALL_TIMEOUT_SECONDS` has elapsed. The
            # cadence is therefore identical to the `:r{stall_window}`
            # key-bucketing this replaces — at most one re-POST per stall
            # window — but expressed as durable state rather than as a
            # clock-derived string smuggled through `batch_index`.
            replan_generation = int(job.planning_generation or 0) + 1
            job.planning_generation = replan_generation
            re_batches = plan_batches(
                resolved_targets,
                http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN,
                http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX,
                browser_max=settings.SCRAPE_BATCH_BROWSER_MAX,
                planning_generation=replan_generation,
            )
            intents = DispatchIntentStore(
                session,
                workspace_id=workspace_id,
                scrape_job_id=job.id,
                authorized_cancellation_generation=job.cancellation_generation or 0,
            )
            # A second client, bound to THIS job's intent store. The outer
            # `client` stays the (stateless) node-liveness prober so one
            # `daemonstatus.json` cache is shared across every job in the
            # sweep, as before.
            dispatch_client = ScrapydDispatchClient(settings=settings, intents=intents)
            # EPA C3: one authorization service per JOB, pinned to that
            # job's workspace. This sweep is FLEET-scoped, so a single
            # service pinned to whichever workspace came first would
            # resolve every later grant against the wrong tenant.
            costauth = CostAuthorizationService(default_workspace_id=workspace_id)

            try:
                for batch in re_batches:
                    # EPA W4.1: same zero-retry skip as `dispatch_job` --
                    # a stall recovery is itself a retry, so a domain that
                    # has since been certified UNSUPPORTED must not be
                    # re-POSTed either.
                    if _skip_unsupported_batch(
                        session,
                        workspace_id=workspace_id,
                        scrape_job_id=job.id,
                        batch=batch,
                    ):
                        continue
                    project, spider, nodes = _batch_route(batch, settings)
                    node_url = select_node(batch.domain, nodes)
                    status_payload = node_status_cache.get(node_url, _UNPROBED)
                    if status_payload is _UNPROBED:
                        status_payload = client.daemon_status(node_url)
                        node_status_cache[node_url] = status_payload
                    depth = (
                        None if status_payload is None else _queue_depth(status_payload)
                    )
                    if depth is not None and depth > 0:
                        # Node alive and working its queue: these targets are
                        # queued behind max_proc/rate limits, not stalled.
                        continue
                    identity = _batch_identity(job.id, batch, project, spider)
                    # Paid dispatch site 6 (RETRY): a stall re-POST is a
                    # SECOND physical fetch of work already paid for once,
                    # so it must clear the gate on its own account. Its
                    # dispatch identity carries the advanced planning
                    # generation (EPA B1), so its dedupe key differs from
                    # the original dispatch's — a retry gets its own grant
                    # rather than silently reusing the first one's.
                    grant = authorize_or_none(
                        costauth,
                        _batch_authorization_request(
                            batch,
                            workspace_id=workspace_id,
                            scrape_job_id=job.id,
                            purpose=AuthorizationPurpose.RETRY,
                            identity=identity,
                        ),
                        site="tasks_jobs.recover_stalled_batches",
                    )
                    if grant is None:
                        # Denied: leave the targets stalled and unstamped.
                        # A later sweep re-offers them once the denial
                        # clears; re-POSTing unauthorized is the failure
                        # mode this whole gate exists to remove.
                        continue
                    intents.plan(
                        identity,
                        match_ids=batch.match_ids,
                        batch_index=f"{batch.batch_index}:r{window}",
                    )
                    try:
                        dispatch_client.schedule(
                            project,
                            spider,
                            workspace_id=str(workspace_id),
                            scrape_job_id=str(job.id),
                            match_ids=batch.match_ids,
                            mode=batch.mode,
                            # The `:r{stall_window}` suffix survives as a
                            # spider/traceability label only; the advanced
                            # planning generation is what now distinguishes a
                            # recovery dispatch from the original (EPA B1).
                            batch_index=f"{batch.batch_index}:r{window}",
                            node_url=node_url,
                            identity=identity,
                            # EPA Phase C F2: the RETRY's own grant, threaded
                            # through exactly as `dispatch_job` does. Omitting
                            # it here left every recovery batch's operations
                            # NULL-linked, so C3's sweeper could not see them
                            # in the ledger, let the lease lapse, and released
                            # a hold while the re-POSTed fetches were still
                            # running — the retry's spend was then never
                            # settled against any budget at all.
                            authorization_id=grant.authorization_id,
                            budget_decision_version=grant.budget_decision_version,
                            entitlement_version=grant.entitlement_version,
                            breaker_decision=grant.breaker_decision,
                        )
                    except Exception:
                        costauth.release(grant.authorization_id)
                        raise
                    # A re-POSTed target's stall clock restarts here — without
                    # a fresh stamp the very next sweep would reap it again,
                    # which is the feedback loop this whole fix removes.
                    #
                    # EPA B2: `stamp_targets_dispatched` is the SINGLE
                    # stamping path (see `dispatch_job`'s call site for the
                    # full rationale) -- `dispatch_client._redis` is the
                    # same Redis client `schedule()` just POSTed the guard
                    # through.
                    batch_match_ids = set(batch.match_ids)
                    stamp_targets_dispatched(
                        session,
                        dispatch_client._redis,  # noqa: SLF001 - same client that just POSTed
                        batch=DispatchedBatch(
                            workspace_id=workspace_id,
                            scrape_job_id=job.id,
                            identity=identity,
                            targets=[
                                target
                                for target in stalled_targets
                                if target.match_id in batch_match_ids
                            ],
                        ),
                        stamp=datetime.now(timezone.utc),
                    )
            finally:
                # F-1 (2026-08-22 review): commit THIS job's stamps before
                # moving on. With one commit after the whole sweep, a
                # single failure anywhere downstream (an unreachable node
                # on batch 50, a job later in the scan) rolled back every
                # stamp earned by batches that really were re-POSTed --
                # and past the 900s Redis guard TTL those targets get
                # re-POSTed again. Same durability hole as `dispatch_job`,
                # same fix: a successful POST's stamp outlives a later
                # failure. Also puts the commit inside the job's own
                # `set_workspace_context`, rather than under whichever
                # workspace happened to be last.
                session.commit()

        session.commit()
