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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app.workers.tasks_dispatch import DispatchedBatch, stamp_targets_dispatched
from app_shared.config import get_settings
from app_shared.costauth import (
    AuthorizationPurpose,
    AuthorizationRequest,
    CostAuthorizationService,
    FirstRungReservation,
    authorize_or_none,
    escalation_reservation,
    first_rung_reservation,
    reservation_rung,
)
from app_shared.database import get_session, get_system_session, set_workspace_context
from app_shared.domains.lifecycle import unsupported_target_outcome
from app_shared.domains.state_lookup import get_domain_state
from app_shared.enums import (
    DispatchIntentState,
    ScrapeJobStatus,
    ScrapeProfileMode,
    ScrapeTargetStatus,
)
from app_shared.ids import new_uuid7
from app_shared.jobs.batching import (
    DEFAULT_STRATEGY_METHOD,
    Batch,
    ResolvedTarget,
    plan_batches,
)
from app_shared.jobs.coalescing import cluster_for_coalescing
from app_shared.jobs.dispatch_intents import (
    OPEN_QUESTION_STATES,
    DispatchIntentStore,
    reconcile_inflight_intents,
    receiver_holds_execution,
)
from app_shared.jobs.reaper import (
    fail_targets_past_job_deadline,
    revert_stale_started_targets,
)
from app_shared.jobs.lifecycle import resolve_finalized_status, stall_window
from app_shared.jobs.reconciliation import reconcile_successful_failed_targets
from app_shared.jobs.node_load import NodePlacement, read_node_loads
from app_shared.jobs.nodes import NodeLoad
from app_shared.jobs.targets import Counts, aggregate_counts, mark_target
from app_shared.messaging import enqueue
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_task
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.netledger.recorder import canonical_url_hash
from app_shared.models.domain_playbooks import DomainPlaybook, DomainState
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyMethod, DomainStrategyProfile
from app_shared.outbox import write_outbox_message
from app_shared.repository import scoped_get, scoped_select
from app_shared.redis_client import get_redis_client
from app_shared.strategy.methods import (
    PlaybookStrategy,
    mode_for_access_method,
    resolve_next_physical_attempt,
)
from app_shared.scrapyd import (
    DispatchIdentity,
    ScrapydDispatchClient,
    build_dispatch_identity,
)
from app_shared.task_names import (
    CREATE_WEBHOOK_EVENT,
    DISPATCH_RECONCILE_INTENTS,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_REAP_STALE_TARGETS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_RECONCILE_FALSE_FAILURES,
    SCRAPE_REDISPATCH_JOBS,
    STRATEGY_STATS_FLUSH,
)
from app_shared.webhooks.payloads import build_job_event
# EPA C4: the per-target physical-attempt gate C1 built. `apps/workers`
# already declares `scrape_core` as a dependency (its pyproject) and this
# module is stdlib + `app_shared.enums` only -- no Scrapy/Twisted enters
# the worker's import closure through it.
from scrape_core.attempt_budget import AttemptBudget

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


def _node_placement(
    settings, probe_client: Any = None, *, unreachable_pool_fallback: bool = False
) -> NodePlacement:
    """The pass-scoped placement decision-maker for one dispatch pass (B6/F11).

    All the policy lives in :class:`~app_shared.jobs.node_load.NodePlacement`;
    what this adds is the **lazy** probe client. A single-node pool never
    reads a load at all (`NodePlacement`'s rule 2), so on today's
    deployments the `ScrapydDispatchClient` below — and the Redis
    connection it opens — is never constructed. `recover_stalled_batches`
    passes its own long-lived prober in, so the sweep keeps one
    `daemonstatus.json` cache across every job it touches.
    """
    holder: dict[str, Any] = {"client": probe_client}

    def _read(nodes: list[str]) -> dict[str, NodeLoad]:
        client = holder["client"]
        if client is None:
            client = ScrapydDispatchClient(settings=settings)
            holder["client"] = client
        return read_node_loads(client, nodes, getattr(client, "_redis", None))

    return NodePlacement(
        max_pending=settings.SCRAPYD_MAX_PENDING_PER_NODE,
        load_reader=_read,
        unreachable_pool_fallback=unreachable_pool_fallback,
    )


def _intent_session_factory(workspace_id: uuid.UUID) -> Callable[[], Any]:
    """A factory of standalone, workspace-scoped sessions (EPA B2 / F06).

    The dispatch-intent store's short-transaction mode opens one session
    per transition, and that session must already be able to see the
    workspace's rows — RLS is enforced per transaction, so a fresh one
    has to re-assert ``app.workspace_id``. Scoping lives here, at the
    injector, rather than inside the store: the maintenance sweep injects
    a BYPASSRLS system session instead and must NOT have a GUC quietly
    set behind it.

    Returns a callable whose result is a **context manager** — the seam
    shape ``drain_outbox`` already uses. It is built out of this module's
    own ``get_session``/``set_workspace_context``, which is also what
    lets the planner-replay harnesses substitute an in-memory session for
    the whole five-step protocol without knowing this function exists.
    """

    @contextmanager
    def _open() -> Iterator[Session]:
        with get_session() as session:
            set_workspace_context(session, workspace_id)
            yield session

    return _open


@dataclass(frozen=True)
class PlannedDispatch:
    """One batch, fully decided in step 1 and not yet sent (EPA B2 / F06).

    Everything the POST needs is fixed *before* the plan commits: the
    route, the node, the identity, the ``jobid`` we chose for the remote
    run, the C3 grant that paid for it, and the exact target rows to
    stamp when the node says yes. Carrying it in one frozen record is
    what makes step 3 a pure network call — it reads nothing from the
    database, so it can run with no transaction open, which is the
    property the whole protocol turns on.
    """

    batch: Batch
    identity: DispatchIdentity
    project: str
    spider: str
    node_url: str
    #: ``dispatch_intents.scrapyd_job_id`` — the name the remote run has
    #: in every attempt, including a re-POST after ``RECONCILED_MISSING``.
    jobid: str
    #: The C3 grant reserved for this batch in step 1's transaction, and
    #: released in the failure path if the batch never reaches Scrapyd.
    grant: Any
    targets: list[ScrapeJobTarget]
    #: True when this batch's durable intent was already in
    #: ``RECONCILED_MISSING`` at plan time — i.e. this POST is a RECOVERY
    #: re-send, not a first dispatch (R11). It is the only case where the
    #: node may already be holding a run under the very ``jobid`` we are
    #: about to send, and Scrapyd 1.6.0 will NOT dedup it for us, so step
    #: 3 asks the node first. A first dispatch skips that round trip.
    recovery: bool = False


def _batch_authorization_request(
    batch: Batch,
    *,
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    purpose: AuthorizationPurpose,
    identity: DispatchIdentity,
) -> AuthorizationRequest:
    """The C3 authorization one batch needs, derived from the plan (EPA C3/C6).

    Three things the planner genuinely knows are used, and nothing is
    invented beyond them:

    * **transport/provider** come from the batch's FIRST RUNG (EPA C6,
      F18) — ``batch.initial_transport``, the ``AccessMethod`` the
      strategy ladder already selected, whose cheap end is the playbook's
      ``cheap_path``. A ``DIRECT`` rung reserves zero bytes and zero
      money, and is *still authorized*: the breaker, the workspace
      entitlement and the concurrency cap all apply to free traffic too.
      Before F18 this was derived from ``mode`` alone and every HTTP
      batch was authorized as ``PROXY`` fail-closed, because the planner
      could not then know the rung. Over-reserving is not free — it burns
      a byte and money ceiling on traffic that never touches a provider,
      denying real paid work later in the same period. When the rung is
      unknown (a batch planned by a caller that attaches no transport —
      every pre-C6 site, including existing tests) the OLD fail-closed
      mode derivation is used unchanged.
    * **size** is the COALESCED count of unique physical requests
      (``batch.unique_physical_requests``), not the match count: three
      matches sharing one canonical URL under one coalescing key are one
      fetch. Falls back to ``len(match_ids)`` when no target carried a
      coalescing identity. It is priced by the MEASURED unit the provider
      actually bills — proxied bytes, plus browser wall-seconds for a
      BROWSER rung (``costauth.pricing``, H4/B2) — never a per-DOMAIN
      request rate: what a fetch costs is what it weighs.
    * **purpose** is the caller's, *upgraded* to an escalation purpose
      when this dispatch is a climb: ``initial_transport`` is a paid rung
      and differs from the playbook's ``cheap_transport``. Escalation
      reserves SEPARATELY, at the full cost of the escalated rung — never
      as a top-up mutating the cheap rung's live grant, which would make
      one grant describe two different physical transports. A
      ``RETRY`` keeps its own purpose: a stall re-POST is a second
      attempt on the rung already authorized, not a new climb, and
      relabelling it would change which C2 degradation rules can deny it.

    ``dedupe_key`` is the batch's dispatch identity key (EPA B1). That is
    exactly the right grain: a duplicate/at-least-once delivery of the
    same batch re-derives the same identity, so it collapses onto the
    grant it already holds instead of reserving the budget twice — the
    same property, on the money side, that the identity already gives the
    POST side.
    """
    escalation_purpose, rung = _batch_first_rung_reservation(batch)
    if escalation_purpose is not None and purpose is not AuthorizationPurpose.RETRY:
        purpose = escalation_purpose
    return AuthorizationRequest(
        workspace_id=workspace_id,
        domain=batch.domain,
        transport=rung.transport,
        provider=rung.provider,
        estimated_bytes=rung.estimated_bytes,
        estimated_cost_micro_units=rung.estimated_cost_micro_units,
        purpose=purpose,
        estimated_requests=rung.estimated_requests,
        estimated_browser_seconds=rung.estimated_browser_seconds,
        scrape_job_id=scrape_job_id,
        dedupe_key=identity.key,
    )


def _batch_unique_requests(batch: Batch) -> int:
    """The batch's coalesced physical-request count, at least 1 (EPA C6).

    ``plan_batches`` computes it from the same ``coalescing_key`` the
    clustering pass uses; ``None`` means no target carried a coalescing
    identity, and the conservative fallback is then one request per match
    — exactly the pre-C6 number.
    """
    coalesced = batch.unique_physical_requests
    if coalesced is None:
        coalesced = len(batch.match_ids)
    return max(1, int(coalesced))


def _batch_first_rung_reservation(
    batch: Batch,
) -> tuple[AuthorizationPurpose | None, FirstRungReservation]:
    """``(escalation purpose or None, amounts)`` for one batch (EPA C6/F18).

    The escalation purpose is non-``None`` only when BOTH facts are known
    and they disagree — the batch's resolved rung is paid and is not the
    playbook's ``cheap_path``. An unknown ``cheap_transport`` yields
    ``None`` (no upgrade), so a domain with no playbook keeps the caller's
    purpose exactly as it had it before C6.

    An ``initial_transport`` the reservation vocabulary does not recognise
    is treated as unknown, not as an error: a strategy row carrying a
    transport this build has never heard of must degrade to the
    fail-closed paid reservation, not crash the dispatch pass.
    """
    requests = _batch_unique_requests(batch)
    rung_name: str | None = None
    if batch.initial_transport is not None:
        try:
            rung_name = reservation_rung(batch.initial_transport)
        except ValueError:
            logger.warning(
                "dispatch: unknown initial_transport=%r on domain=%s -- reserving "
                "fail-closed from mode instead",
                batch.initial_transport,
                batch.domain,
            )
    if rung_name is None:
        # Pre-F18 fail-closed derivation: the planner does not know the
        # rung, so it assumes the paid one.
        rung_name = (
            "BROWSER" if batch.mode == ScrapeProfileMode.BROWSER else "PROXY"
        )
        return None, first_rung_reservation(
            initial_transport=rung_name, unique_requests=requests
        )

    cheap_name: str | None = None
    if batch.cheap_transport is not None:
        try:
            cheap_name = reservation_rung(batch.cheap_transport)
        except ValueError:
            cheap_name = None
    if cheap_name is not None and rung_name != cheap_name and rung_name != "DIRECT":
        return escalation_reservation(
            to_transport=rung_name, unique_requests=requests
        )
    return None, first_rung_reservation(
        initial_transport=rung_name, unique_requests=requests
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


#: Statuses a budget/deadline refusal may finalize. Deliberately the two
#: NON-terminal pickup states only: a target that already reached
#: COMPLETED/FAILED/SKIPPED keeps the outcome it earned — a refusal is a
#: reason not to START work, never a reason to overwrite a finished
#: result (`mark_target`'s `only_if_status` is the same guard the rest of
#: this module uses for exactly that reason).
_BUDGET_REFUSAL_ELIGIBLE_STATUSES = (
    ScrapeTargetStatus.PENDING,
    ScrapeTargetStatus.DEFERRED,
)


def _target_attempt_budget(
    redis: Any | None,
    *,
    settings: Any,
    scrape_job_id: uuid.UUID | None,
    target: ScrapeJobTarget,
    domain: str,
    playbook: PlaybookStrategy | None,
) -> AttemptBudget | None:
    """This target's physical-attempt gate, or `None` when unwired.

    EPA C4, closing C1's carry-forward. `None` (no Redis client, or a
    caller with no job id) means the ladder runs exactly as it did before
    C1 — the gate is absent, not permissive-by-accident.

    The deadline is anchored on the target's OWN clock (`started_at`,
    else `created_at`, else now), not on the job's: `SCRAPE_TARGET_DEADLINE_
    SECONDS` is a per-target bound, and anchoring it on a 12-hour job
    would make it unreachable. A re-plan therefore re-derives the SAME
    deadline instead of granting a fresh one, which is the whole point of
    a wall-clock bound.

    `recovery_probe_fraction` prefers the domain's own playbook value over
    the fleet setting: a domain under active repair wants a different
    probe rate from the fleet default, and that is precisely what the
    C4 column exists to express.
    """
    if redis is None or scrape_job_id is None:
        return None
    anchor = (
        getattr(target, "started_at", None)
        or getattr(target, "created_at", None)
        or datetime.now(timezone.utc)
    )
    fraction = (
        playbook.recovery_probe_fraction
        if playbook is not None and playbook.recovery_probe_fraction is not None
        else settings.SCRAPE_RECOVERY_PROBE_FRACTION
    )
    return AttemptBudget(
        redis,
        job_id=scrape_job_id,
        match_id=target.match_id,
        max_physical=settings.SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS,
        deadline_at=anchor + timedelta(seconds=settings.SCRAPE_TARGET_DEADLINE_SECONDS),
        domain=domain,
        recovery_probe_fraction=fraction,
    )


def _load_playbook_strategies(
    session: Session, domains: Iterable[str]
) -> dict[str, PlaybookStrategy]:
    """`{domain: PlaybookStrategy}` for `domains`, in ONE bounded query.

    EPA C4. `domain_playbooks` is fleet-wide, operator-curated reference
    data with no `workspace_id` (see `app_shared.models.domain_playbooks`),
    so this is a plain `select` — the sanctioned path for that table, the
    same one `app_shared.strategy.resolution` already uses.

    One query for the whole dispatch pass, never one per target
    (Principle IV): a job routinely carries thousands of targets across a
    handful of domains. A domain with no row is simply absent from the
    result, and the ladder then runs with no strategy hints — which is
    exactly the pre-C4 behaviour.
    """
    wanted = {domain for domain in domains if domain}
    if not wanted:
        return {}
    strategies: dict[str, PlaybookStrategy] = {}
    for row in (
        session.execute(select(DomainPlaybook).where(DomainPlaybook.domain.in_(wanted)))
        .scalars()
        .all()
    ):
        strategy = PlaybookStrategy.from_row(row)
        if strategy is not None:
            strategies[row.domain] = strategy
    return strategies


def _resolve_domains_and_modes(
    session: Session,
    workspace_id: uuid.UUID | str,
    targets: list[ScrapeJobTarget],
    *,
    scrape_job_id: uuid.UUID | None = None,
    redis: Any | None = None,
    settings: Any | None = None,
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

    **EPA C4 — the attempt budget and the versioned strategy.** C1 built
    the per-target physical-attempt gate but left it inert: nothing
    constructed an `AttemptBudget`, so `budget=None` reached the ladder
    from every call site (`reports/C1.md`, "Wiring is deliberately partial").
    This is one of the two sites that fixes that. When `redis` and
    `scrape_job_id` are supplied, each target with no durable cursor gets
    a budget built from `SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS`, its own
    deadline (`started_at`/`created_at` + `SCRAPE_TARGET_DEADLINE_SECONDS`)
    and its domain's `recovery_probe_fraction`, and the ladder both
    charges and gates against it. A terminal refusal
    (`ATTEMPT_BUDGET_EXHAUSTED`/`TARGET_DEADLINE_EXCEEDED`) marks the
    target FAILED with that exact code and drops it from the plan — a
    target that may not fetch must not be dispatched, and it must say why.

    They are OPTIONAL parameters, not required ones, so the reaper's
    re-plan path and every existing unit test keep working with
    `budget=None`: an unwired caller gets exactly the pre-C4 behaviour
    (the deadline is the bound that matters and it is re-derived every
    pass), rather than a crash or a silently different plan.

    The playbook lookup is one query for the whole pass
    (:func:`_load_playbook_strategies`), never one per target.
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

    # EPA C4: one bounded read for every domain in this pass.
    playbooks = _load_playbook_strategies(session, domains.values())
    settings = settings if settings is not None else get_settings()

    resolved: list[ResolvedTarget] = []
    cursor_advanced = False
    for target in targets:
        match = matches.get(target.match_id)
        if match is None:
            continue
        domain = domains.get(match.competitor_id)
        if domain is None:
            continue
        playbook = playbooks.get(domain)
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
                budget = _target_attempt_budget(
                    redis,
                    settings=settings,
                    scrape_job_id=scrape_job_id,
                    target=target,
                    domain=domain,
                    playbook=playbook,
                )
                decision = resolve_next_physical_attempt(
                    methods_by_profile.get(strategy_profile.id, ()),
                    preferred_method_id=strategy_profile.preferred_method_id,
                    current_attempt_ordinal=target.strategy_attempt_ordinal,
                    budget=budget,
                    playbook=playbook,
                )
                if decision.refusal is not None:
                    # Terminal for the TARGET (EPA C1's codes): it has
                    # spent its physical-attempt budget or run past its
                    # own deadline. Finalize it with that exact code and
                    # drop it from the plan -- dispatching a target the
                    # ladder just refused would spend the money the
                    # budget exists to stop, and finalizing it silently
                    # would leave the strategy optimizer with no reason.
                    logger.info(
                        "tasks_jobs: attempt_budget_refusal job=%s match=%s "
                        "domain=%s code=%s strategy_version=%s",
                        scrape_job_id,
                        target.match_id,
                        domain,
                        decision.refusal.value,
                        decision.strategy_version,
                    )
                    mark_target(
                        session,
                        workspace_id=workspace_id,
                        scrape_job_id=target.scrape_job_id,
                        match_id=target.match_id,
                        status=ScrapeTargetStatus.FAILED,
                        error_code=decision.refusal,
                        only_if_status=_BUDGET_REFUSAL_ELIGIBLE_STATUSES,
                    )
                    continue
                selection = decision.selection
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
                # EPA W4.3: the SAME canonicalization the network ledger
                # groups on (`app_shared.netledger.recorder.
                # canonical_url_hash`), never a second one. Attached
                # unconditionally -- it is cheap and pure, and
                # `plan_batches` never reads this field, so computing it
                # here changes nothing about `plan_batches`'s output.
                # Only `cluster_for_coalescing`, called behind
                # `JOBS_COALESCING_ENABLED` below, ever reads it.
                canonical_url_hash=canonical_url_hash(match.normalized_competitor_url),
                # EPA C4: the rest of the coalescing equivalence key that
                # this layer can honestly answer for. `workspace_id` makes
                # the key complete (and lets `coalesced_groups` refuse a
                # mixed-workspace input); `transport` splits clusters whose
                # resolved rungs would not have produced the same bytes.
                # `region`/`currency`/`proxy_country`/`variant_selector_hash`
                # are resolved further down (the spider's access-policy and
                # variant resolution, `scrape_core.targets.load_targets`) and
                # are deliberately left `None` here rather than guessed --
                # an invented component would MERGE nothing but could make
                # two genuinely different fetches look alike.
                workspace_id=(
                    workspace_id
                    if isinstance(workspace_id, uuid.UUID)
                    else uuid.UUID(str(workspace_id))
                ),
                transport=(
                    getattr(
                        selected_method.access_method,
                        "value",
                        selected_method.access_method,
                    )
                    if selected_method is not None
                    else None
                ),
                # EPA C6 (F18): the playbook's cheap rung for this domain,
                # carried so the dispatch site can tell a FIRST attempt
                # (reserve the cheap rung — zero bytes when it is DIRECT)
                # from an ESCALATION (a separate grant, under its own
                # purpose). `None` when the domain has no playbook, which
                # keeps the pre-F18 fail-closed reservation.
                cheap_path=(
                    None
                    if playbook is None or playbook.cheap_path is None
                    else str(
                        getattr(playbook.cheap_path, "value", playbook.cheap_path)
                    )
                ),
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
            session,
            workspace_uuid,
            targets,
            # EPA C4: wire C1's attempt budget (it was inert until a
            # caller passed one) and this domain's versioned strategy.
            scrape_job_id=job.id,
            redis=get_redis_client(),
            settings=settings,
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

        # EPA W4.3: reorder same-canonical-URL targets to be contiguous
        # before chunking, so a cluster lands in one dispatch chunk
        # instead of splitting across one by accident of input order
        # (`app_shared.jobs.coalescing` module docstring). ON by default
        # since EPA plan task B4 (2026-09-04) -- `JOBS_COALESCING_ENABLED`
        # can still be set `False` to fall back to a no-op, where
        # `plan_batches` receives `resolved_targets` in its original
        # order, exactly as before W4.3.
        planning_targets = (
            cluster_for_coalescing(resolved_targets)
            if settings.JOBS_COALESCING_ENABLED
            else resolved_targets
        )
        batches = plan_batches(
            planning_targets,
            http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN,
            http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX,
            browser_max=settings.SCRAPE_BATCH_BROWSER_MAX,
            planning_generation=planning_generation,
        )

        # --- STEP 1: plan, authorize, claim -- then COMMIT --------------------
        # The whole plan becomes durable BEFORE a single byte leaves this
        # process (F06). Three things land in this one transaction:
        #
        #   * one `dispatch_intents` row per batch, PLANNED, carrying the
        #     node it will go to and the `scrapyd_job_id` WE chose for it
        #     (a uuid5 over the identity -- see
        #     `deterministic_scrapyd_job_id`);
        #   * `claimed_at` on every target those batches carry, so the
        #     phase clock (`created_at -> dispatched_at`, `claimed_at ->
        #     remote_accepted_at`) measures the POST round trip and not
        #     the planning pass;
        #   * the strategy-cursor advance and `planning_generation` that
        #     name the plan.
        #
        # The store is given a `session_factory` rather than this session:
        # from here on every intent transition commits in its OWN short
        # transaction, which is what lets step 3 run with no transaction
        # open at all.
        intents = DispatchIntentStore(
            session,
            workspace_id=workspace_uuid,
            scrape_job_id=job.id,
            authorized_cancellation_generation=job.cancellation_generation or 0,
        )
        # EPA C3: the cost-authorization gate. One service per task
        # invocation; it opens its own short transaction per grant, which
        # is deliberate -- the reservation must be DURABLE before the POST
        # for the same reason the dispatch intent is, and joining this
        # task's long transaction would tie every workspace's budget row
        # to the lifetime of one job's dispatch loop.
        costauth = CostAuthorizationService(default_workspace_id=workspace_uuid)
        planned: list[PlannedDispatch] = []
        claim_stamp = datetime.now(timezone.utc)
        targets_by_match: dict[Any, list[ScrapeJobTarget]] = {}
        for target in targets:
            targets_by_match.setdefault(target.match_id, []).append(target)

        placement = _node_placement(settings)
        for batch in batches:
            project, spider, nodes = _batch_route(batch, settings)
            identity = _batch_identity(job.id, batch, project, spider)
            # EPA W4.1: a domain certified UNSUPPORTED is a
            # product-visible, zero-retry skip -- checked before C3's
            # gate (see `_skip_unsupported_batch`'s docstring for why
            # the ordering is cheaper and still composes with C3's own
            # batch-level UNSUPPORTED denial). Hoisted into the planning
            # pass by B2: a batch nobody will ever POST should not get an
            # intent row either.
            if _skip_unsupported_batch(
                session,
                workspace_id=workspace_uuid,
                scrape_job_id=job.id,
                batch=batch,
            ):
                continue
            # EPA B6 (F11): WHERE this batch runs, decided before it is
            # paid for. `None` means every node in the pool is
            # unreachable or already `SCRAPYD_MAX_PENDING_PER_NODE` deep.
            node_url = placement.place(
                domain=batch.domain,
                nodes=nodes,
                intents=intents,
                identity=identity,
            )
            if node_url is None:
                # DEFERRED, not denied and not failed: no intent row, no
                # grant, no `claimed_at`, no POST. The targets stay
                # PENDING/unstamped, so `redispatch_pending_jobs` offers
                # them again once a node drains -- the same shape as a
                # cost-authorization denial below, and for the same
                # reason: POSTing onto a full node does not make the work
                # run sooner, it just holds a grant and a phase clock open
                # while it queues.
                logger.info(
                    "dispatch: DEFERRED batch domain=%s mode=%s -- every node in "
                    "the pool is saturated or unreachable "
                    "(max_pending=%d) workspace_id=%s scrape_job_id=%s",
                    batch.domain,
                    batch.mode,
                    settings.SCRAPYD_MAX_PENDING_PER_NODE,
                    workspace_uuid,
                    job.id,
                )
                continue
            # R11: ask the durable ledger BEFORE reserving anything. An
            # intent that is an open question -- `POSTED` (a request may
            # be on the wire right now) or `RECONCILED_AMBIGUOUS` (the
            # sweep looked and could not establish what happened) -- must
            # not be re-planned at all. The POST itself would be refused
            # further down (the Redis claim, and `schedule()`'s step 0b
            # reconcile gate), but the C3 grant is reserved HERE, several
            # steps before either of those runs, and a reservation taken
            # for a dispatch that is then refused is a second live hold
            # against the same logical batch. "Exactly one cost
            # authorization per logical job" is a property of the order
            # these two things happen in, not of the gates downstream.
            durable_state = intents.current_state(identity)
            if durable_state in OPEN_QUESTION_STATES:
                logger.info(
                    "dispatch: SKIPPED batch domain=%s mode=%s -- its durable "
                    "intent is %s (unsettled); not authorizing or re-POSTing "
                    "until the reconciler resolves it workspace_id=%s "
                    "scrape_job_id=%s",
                    batch.domain,
                    batch.mode,
                    durable_state,
                    workspace_uuid,
                    job.id,
                )
                continue
            # Paid dispatch site 1 (REFRESH) and site 2
            # (BROWSER_ESCALATION) -- the same pass, distinguished by
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
                # Denied: do NOT plan, do NOT POST and do NOT stamp. The
                # targets stay PENDING/unstamped, so `redispatch_pending_jobs`
                # will offer them again once whatever denied them
                # (budget, breaker, entitlement, domain state) clears.
                # A denial must never look like a dispatch.
                continue
            intent = intents.plan(
                identity,
                match_ids=batch.match_ids,
                batch_index=batch.batch_index,
                node_url=node_url,
            )
            batch_targets = [
                target
                for match_id in batch.match_ids
                for target in targets_by_match.get(match_id, ())
            ]
            for target in batch_targets:
                target.claimed_at = claim_stamp
            planned.append(
                PlannedDispatch(
                    batch=batch,
                    identity=identity,
                    project=project,
                    spider=spider,
                    node_url=node_url,
                    jobid=str(intent.scrapyd_job_id),
                    grant=grant,
                    targets=batch_targets,
                    recovery=(
                        durable_state == DispatchIntentState.RECONCILED_MISSING
                    ),
                )
            )

        # The plan is durable here, or it did not happen. Committing also
        # closes this session's transaction, which is a precondition of
        # step 3 below -- the POST must not be issued with a Postgres
        # backend pinned open behind it.
        session.commit()

        # Steps 2-4 run per batch through a store that owns its own short
        # transactions, so `POSTED` is committed before the POST and
        # `CONFIRMED` is committed after it.
        dispatching_intents = DispatchIntentStore(
            None,
            workspace_id=workspace_uuid,
            scrape_job_id=job.id,
            authorized_cancellation_generation=job.cancellation_generation or 0,
            session_factory=_intent_session_factory(workspace_uuid),
        )
        client = ScrapydDispatchClient(settings=settings, intents=dispatching_intents)
        undispatched = list(planned)
        try:
            for item in planned:
                # --- STEPS 2 + 3: record_post (commit) -> POST ------------
                # `client.schedule()` marks the intent POSTED through the
                # store above -- its own transaction, committed -- and only
                # then issues the network call, with nothing of ours open.
                # A worker killed anywhere in here leaves a committed
                # POSTED row naming the node and the jobid, which is
                # exactly what `reconcile_inflight_intents` settles.
                #
                # --- STEP 2-pre (R11): execution identity, at the receiver
                # A recovery re-POST is the one case where the node may
                # ALREADY hold a run under the exact `jobid` we are about
                # to send, and Scrapyd 1.6.0 does not protect us from
                # that: its `Schedule.render_POST` hands the supplied
                # `jobid` to the scheduler as `_job` with no dedup, so a
                # duplicate queues a SECOND run under a colliding name.
                # The deterministic id makes a re-POST traceable; only
                # this check makes it idempotent. Ask the one node the
                # intent recorded, with no transaction open.
                if item.recovery:
                    holds = receiver_holds_execution(
                        client,
                        node_url=item.node_url,
                        node_class=item.identity.node_class,
                        scrapyd_job_id=item.jobid,
                    )
                    if holds is not False:
                        # True: the run is there -- adopt it and never
                        # send a second copy. None: the node could not be
                        # asked, and absence of an answer is not an
                        # answer. Both give the grant straight back: this
                        # batch spends nothing on this pass, and its
                        # targets stay unstamped so a later pass (by
                        # which time the intent is CONFIRMED, or the
                        # node is reachable) offers them again.
                        if holds:
                            dispatching_intents.confirm(item.identity, item.jobid)
                        logger.warning(
                            "dispatch: RECOVERY NOT RE-POSTED jobid=%s node_url=%s "
                            "receiver_holds=%s workspace_id=%s scrape_job_id=%s",
                            item.jobid,
                            item.node_url,
                            holds,
                            workspace_uuid,
                            job.id,
                        )
                        costauth.release(item.grant.authorization_id)
                        undispatched.remove(item)
                        continue
                try:
                    client.schedule(
                        item.project,
                        item.spider,
                        workspace_id=str(workspace_uuid),
                        scrape_job_id=str(job.id),
                        match_ids=item.batch.match_ids,
                        mode=item.batch.mode,
                        # Spider argument + traceability label ONLY -- the
                        # idempotency decision is `identity`'s (EPA B1).
                        batch_index=item.batch.batch_index,
                        node_url=item.node_url,
                        identity=item.identity,
                        # EPA B2: the remote run's name, chosen at plan
                        # time and already committed on the intent. A
                        # re-POST re-derives the same value, so Scrapyd
                        # dedups rather than double-running the batch.
                        jobid=item.jobid,
                        # EPA C4b: stamp this batch's C3 grant onto the
                        # spider so its own network-ledger boundary can
                        # trace every physical operation back to it.
                        authorization_id=item.grant.authorization_id,
                        # EPA Phase C F3: and WHAT that grant decided, so
                        # C1's three decision columns stop being NULL.
                        budget_decision_version=item.grant.budget_decision_version,
                        entitlement_version=item.grant.entitlement_version,
                        breaker_decision=item.grant.breaker_decision,
                    )
                except Exception:
                    # Failure BEFORE dispatch: nothing was spent, so the
                    # whole hold goes back immediately rather than waiting
                    # out its lease. `release` is CAS-idempotent, so a
                    # retry of this task cannot double-credit the budget.
                    costauth.release(item.grant.authorization_id)
                    undispatched.remove(item)
                    raise
                undispatched.remove(item)
                # --- STEP 4: confirm + stamp, in one committed transaction --
                # `client.schedule()` has already committed CONFIRMED
                # through its own store; this commits the target side of
                # the same fact.
                #
                # F-2 (2026-08-22): stamp the batch's targets the moment they
                # leave here, so the next dispatch delivery cannot re-plan
                # them. A guard-deduped "already scheduled" return counts as
                # dispatched too -- the POST that guard is standing in for did
                # happen.
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
                accepted_at = datetime.now(timezone.utc)
                stamp_targets_dispatched(
                    session,
                    client._redis,  # noqa: SLF001 - the same client just POSTed through
                    batch=DispatchedBatch(
                        workspace_id=workspace_uuid,
                        scrape_job_id=job.id,
                        identity=item.identity,
                        targets=item.targets,
                    ),
                    stamp=accepted_at,
                )
                for target in item.targets:
                    # The other half of the A5 phase clock: `claimed_at`
                    # was written in step 1, and this is when the node
                    # said yes. Together they measure the POST round trip
                    # and nothing else.
                    target.remote_accepted_at = accepted_at
                # F-1 (2026-08-22 review): a stamp is only worth what it
                # survives. With one commit after the whole loop, batch
                # 50's unreachable node used to roll back the stamps of
                # batches 1-49 that really WERE POSTed -- and once the
                # 900s Redis guard expired, the next dispatch delivery
                # re-planned every one of them. That is the exact 2.71x
                # mechanism this phase exists to remove. Committing per
                # batch (B2) is the stronger form of the same rule, and
                # it is also what keeps the next POST from running with a
                # transaction open.
                session.commit()
        except Exception:
            # Grants reserved in step 1 for batches this pass never got to
            # are released rather than left to age out of their lease --
            # a task that raised on batch 3 must not hold batches 4..N's
            # budget until the reaper notices.
            for item in undispatched:
                costauth.release(item.grant.authorization_id)
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


def _interleave_by_workspace(
    refs: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Round-robin ``(job_id, workspace_id)`` refs across workspaces.

    EPA B3/F07. Takes one job from each workspace in turn, then a second
    from each, and so on — so the first N entries cover N distinct
    workspaces (when that many have work) instead of N jobs belonging to
    whichever tenant the scan happened to return first.

    Order WITHIN a workspace is preserved exactly as the scan produced
    it, so this changes only which tenant's turn it is, never which of a
    tenant's own jobs is considered first. Total length is unchanged: a
    sweep that reads this list to the end still sees every ref.
    """
    buckets: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID]]] = {}
    for job_id, workspace_id in refs:
        buckets.setdefault(workspace_id, []).append((job_id, workspace_id))
    if not buckets:
        return []
    ordered: list[tuple[uuid.UUID, uuid.UUID]] = []
    for index in range(max(len(items) for items in buckets.values())):
        for items in buckets.values():
            if index < len(items):
                ordered.append(items[index])
    return ordered


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

    **Work-level fairness (EPA B3/F07).** The scan used to be walked in
    whatever order Postgres returned it, which for a backlogged tenant is
    that tenant's jobs, first and all of them. One workspace with 400
    wedged jobs therefore filled every tick and a second tenant's single
    stuck job waited behind the whole backlog. Two changes fix that
    without changing what gets re-dispatched: the refs are interleaved
    round-robin across workspaces (`_interleave_by_workspace`), and each
    workspace re-enqueues at most
    `SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK` jobs per tick. The
    cap counts *actual re-enqueues*, not rows examined, so a workspace
    whose jobs mostly need nothing does not spend its budget on them.
    The remainder is picked up by the next tick, in the same order.
    """
    per_workspace_cap = int(
        get_settings().SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK
    )
    dispatched_per_workspace: dict[uuid.UUID, int] = {}
    with get_session() as session:
        for job_id, workspace_id in _interleave_by_workspace(
            _scan_job_refs(_NON_TERMINAL_JOB_STATUSES)
        ):
            if dispatched_per_workspace.get(workspace_id, 0) >= per_workspace_cap:
                continue
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
                dispatched_per_workspace[workspace_id] = (
                    dispatched_per_workspace.get(workspace_id, 0) + 1
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
@app.task(name=DISPATCH_RECONCILE_INTENTS)
def reconcile_dispatch_intents() -> None:
    """Settle every `POSTED` dispatch intent against the node it names.

    EPA B3 (2026-09-07), closing the wiring B2 owed. B2 built step 5 of
    the commit-before-send protocol
    (`app_shared.jobs.dispatch_intents.reconcile_inflight_intents`) but
    deliberately stopped short of scheduling it — that meant touching the
    scheduler's beat loop, which B2 did not own. Until this task existed,
    a worker killed between its POST and the node's answer left a row in
    `POSTED` that nothing ever settled: it is not confirmable (nobody
    asked the node) and it is not re-postable either, because only
    `RECONCILED_MISSING` authorizes a re-POST. The whole point of
    committing the intent before sending it is that the knowledge
    survives the death; this is the sweep that acts on it.

    Fleet-wide, on the BYPASSRLS system sessionmaker: a `POSTED` row's
    tenant is exactly what a crashed worker did not get to tell anyone,
    so the sweep cannot be scoped to one. `reconcile_inflight_intents`
    commits each verdict in its own short transaction, so one unreachable
    node cannot roll back the rows already settled — and a node that
    cannot be reached at all leaves its rows `POSTED` for the next pass,
    because absence of an answer is not evidence of absence.

    Driven by the DURABLE `dispatch_reconcile` cadence
    (`DISPATCH_RECONCILE_INTERVAL_SECONDS`, 300s) rather than one of the
    in-process 60s accumulators — see `CADENCE_DISPATCH_RECONCILE`.
    Bounded at `DISPATCH_RECONCILE_LIMIT` rows per pass so it stays well
    inside its 300s Celery time limit; the next tick continues.
    """
    settings = get_settings()
    client = ScrapydDispatchClient(settings=settings)
    report = reconcile_inflight_intents(
        get_system_session,
        client,
        limit=int(settings.DISPATCH_RECONCILE_LIMIT),
        now=datetime.now(timezone.utc),
        # R11: the four `DISPATCH_RECONCILE_ABSENCE_*` knobs -- the
        # in-flight lease, the corroboration quorum and window, and the
        # horizon past which a node's bounded history cannot testify.
        # Passed explicitly rather than re-read inside the sweep so the
        # pass runs under one snapshot of the policy.
        settings=settings,
    )
    logger.info(
        "reconcile_dispatch_intents: examined=%d confirmed=%d missing=%d "
        "unreachable=%d in_flight=%d ambiguous=%d",
        report.examined,
        report.confirmed,
        report.missing,
        report.unreachable,
        report.in_flight,
        report.ambiguous,
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
        # EPA B6 (F11): one placement decision-maker for the WHOLE sweep,
        # not one per job. The pool is fleet-wide, so the batches this
        # sweep has already placed must count against the next job's
        # choice too -- a per-job instance would let ten recovering jobs
        # each reserve the same "least loaded" node.
        placement = _node_placement(
            settings, probe_client=client, unreachable_pool_fallback=True
        )

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
                session,
                workspace_id,
                stalled_targets,
                # A stall re-plan is dispatch too, and the budget/deadline
                # must bound it exactly as they bound the first pass --
                # otherwise a target could escape its ceiling simply by
                # stalling. The deadline is anchored on the TARGET's own
                # clock, so a re-plan re-derives the same instant rather
                # than granting a fresh window.
                scrape_job_id=job.id,
                redis=get_redis_client(),
                settings=settings,
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
            # EPA W4.3: same reordering as the primary dispatch path, same
            # no-op fallback when `JOBS_COALESCING_ENABLED` is set False
            # (ON by default since EPA plan task B4, 2026-09-04).
            replan_targets = (
                cluster_for_coalescing(resolved_targets)
                if settings.JOBS_COALESCING_ENABLED
                else resolved_targets
            )
            re_batches = plan_batches(
                replan_targets,
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
                    identity = _batch_identity(job.id, batch, project, spider)
                    # EPA B6 (F11): the recovery re-POST is placed by the
                    # same capacity-aware rule as the primary path --
                    # sharing this sweep's one `daemonstatus.json` probe
                    # (`placement` is built on the outer `client`, whose
                    # cache is fleet-wide and 10 s deep). A re-plan
                    # advances `planning_generation`, so this is a NEW
                    # identity with no row yet and it genuinely chooses;
                    # a redelivery of this same sweep re-reads the node
                    # off the row it already wrote.
                    # `unreachable_pool_fallback=True`: a pool where NO
                    # node answers is the reaper's own precondition (F-2,
                    # "every node this suite reaps is DEAD"), not a
                    # capacity answer -- it re-POSTs by `select_node` and
                    # lets the POST fail if the pool really is gone. Only
                    # a reachable-but-FULL pool defers here.
                    node_url = placement.place(
                        domain=batch.domain,
                        nodes=nodes,
                        intents=intents,
                        identity=identity,
                    )
                    if node_url is None:
                        # Every node saturated or unreachable. A stall
                        # recovery is the LAST thing that should force
                        # work onto a full node: these targets have
                        # already waited out `SCRAPE_STALL_TIMEOUT_
                        # SECONDS` once, and a POST that only lengthens a
                        # queue buys nothing. The next sweep re-offers
                        # them.
                        logger.info(
                            "recover_stalled_batches: DEFERRED re-POST domain=%s "
                            "mode=%s -- every node saturated or unreachable "
                            "(max_pending=%d) workspace_id=%s scrape_job_id=%s",
                            batch.domain,
                            batch.mode,
                            settings.SCRAPYD_MAX_PENDING_PER_NODE,
                            workspace_id,
                            job.id,
                        )
                        continue
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
                    # EPA B2: the recovery re-plan records its node and
                    # mints its own deterministic `scrapyd_job_id` exactly
                    # as the primary path does, so a worker killed mid-POST
                    # here is reconcilable too.
                    recovery_intent = intents.plan(
                        identity,
                        match_ids=batch.match_ids,
                        batch_index=f"{batch.batch_index}:r{window}",
                        node_url=node_url,
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
                            # EPA B2: the same-id property the primary
                            # path has -- a recovery re-POST of this
                            # identity always carries this jobid.
                            jobid=str(recovery_intent.scrapyd_job_id),
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


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=SCRAPE_REAP_STALE_TARGETS)
def reap_stale_targets() -> None:
    """`SCRAPE_REAP_STALE_TARGETS` (`maintenance` queue, EPA A3/B2).

    Un-wedges jobs abandoned mid-flight, in two passes over
    `app_shared.jobs.reaper`.

    **The failure this closes.** A scrapyd container is replaced mid-job
    — a redeploy, an OOM kill, a drained node. Every target it had
    already claimed stays `STARTED`, and the process that would have
    written the terminal status is gone, so nothing ever writes it:
    `finalize_jobs` never sees "all targets terminal", the job dangles
    `RUNNING` forever, and the customer's refresh silently never
    completes. Neither existing sweep covers that state —
    `recover_stalled_batches` owns only targets still bare `PENDING`, and
    `redispatch_pending_jobs` only jobs holding `PENDING`/`DEFERRED`
    work — so before this task nothing in the system could ever resolve
    a `STARTED` orphan.

    **Pass 1** reverts targets `STARTED` for longer than
    `SCRAPE_STARTED_REAP_AFTER_SECONDS` back to `PENDING` with their
    dispatch stamps cleared, putting them back in front of the ordinary
    dispatcher. The threshold is the browser lock TTL plus a grace, so
    every row it touches has provably outlived its claimant's own
    in-flight lock.

    **Pass 2** is the backstop for what pass 1 cannot fix (a target that
    keeps being re-dispatched and re-lost, or a domain gone permanently
    unreachable): once a job has been `RUNNING` past
    `SCRAPE_JOB_MAX_RUNTIME_SECONDS`, every non-terminal target of it is
    failed `JOB_DEADLINE_EXCEEDED` and stamped `completed_at`, which
    makes the job finalizable on the next `finalize_jobs` sweep.

    Order is deliberate and both passes share one `now`: reverting first
    means a target this tick rescues from `STARTED` is still visible to
    the deadline pass, so an expired job's rows are closed out in the
    same transaction rather than being handed back to the dispatcher for
    another 12 hours.

    FLEET-scoped and run on the BYPASSRLS system session. A wedged job in
    any workspace is exactly the thing being fixed, and under FORCE ROW
    LEVEL SECURITY the ordinary role's unscoped sweep would fail closed
    to zero rows (the mushtryati F-1 failure mode) — silently doing
    nothing, forever, which is indistinguishable from the bug.

    Idempotent and no-arg: a duplicate delivery re-runs both statements,
    whose `WHERE` clauses no longer match the rows the first delivery
    moved.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    with get_system_session() as session:
        reverted = revert_stale_started_targets(
            session,
            now=now,
            older_than_seconds=settings.SCRAPE_STARTED_REAP_AFTER_SECONDS,
        )
        deadline_failed = fail_targets_past_job_deadline(
            session,
            now=now,
            max_runtime_seconds=settings.SCRAPE_JOB_MAX_RUNTIME_SECONDS,
        )
        session.commit()

    logger.info(
        "maintenance_reap_stale_targets reverted=%d deadline_failed=%d",
        reverted,
        deadline_failed,
    )
