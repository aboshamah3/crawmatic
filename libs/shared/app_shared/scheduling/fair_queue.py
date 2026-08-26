"""Two-plane admission control + weighted fair queuing for the scheduler pass.

EPA W4.2 (report §6). This module is the **scheduler plane**: it decides
*which* of the due work a pass may even attempt, and *in what order*. It
deliberately spends nothing and authorizes nothing — that is C3's job
(:class:`app_shared.costauth.service.CostAuthorizationService`), and there
is exactly one budget authority in this system.

The two planes
--------------
1. **Fleet plane — per-domain, across ALL tenants.** The bug this fixes is
   workspace-scoped multiplication of merchant traffic: with a per-
   workspace concurrency cap of 4 and fifty workspaces monitoring the same
   merchant, that merchant sees two hundred concurrent fetches from us and
   is entirely right to block the fleet. A domain's cap is a property of
   the *domain*, so it is enforced on a counter keyed by domain and shared
   by every tenant. The occupancy this cap is compared against is read
   from C3's own live reservations (see
   ``apps/scheduler/app/scheduler/scheduler_app.py::
   read_fleet_domain_usage``) rather than from a counter of our own —
   composing with the single authority instead of becoming a second one.
2. **Tenant plane — weighted fair share of the pass.** Within whatever the
   fleet plane permits, the pass's slots are handed out by *deficit
   weighted round robin* across workspaces, so a workspace with ten
   thousand due rules cannot consume the whole pass while a workspace with
   two due rules waits for a quiet minute that never comes.

Order of operations, and why it is that order
---------------------------------------------
``priority ordering -> admission -> authorization -> dispatch``

Freshness urgency (:func:`freshness_urgency`) reorders a workspace's queue
so the stalest, most overdue work is attempted first. It is **ordering
only**. It cannot raise a domain's cap, cannot raise the fleet cap, cannot
grant a slot the batch limit has already spent, and — most importantly —
cannot skip the authorization gate: :func:`execute_plan` calls ``gate``
for every admitted candidate and dispatches only what the gate returns
without raising. An urgent item whose authorization is denied is denied,
and shows up in :attr:`PassOutcome.denied`, never in
:attr:`PassOutcome.dispatched`.

Poison isolation, bounded retries, dead-letter, replay
-------------------------------------------------------
The pre-W4.2 refresh pass reacted to a raising rule by aborting the whole
pass (``break``), on the sound reasoning that an unchanged ``next_run_at``
would otherwise re-select the same rule forever. The cost of that
correctness is that **one poison rule stops every other tenant's
scheduling** for as long as it keeps poisoning. :class:`RetryLedger`
removes the trade: a raising item is isolated to itself, retried a bounded
number of times, and then dead-lettered out of the candidate set so the
pass proceeds. Dead letters are inspectable
(:meth:`RetryLedger.dead_letters`) and replayable
(:meth:`RetryLedger.replay` / :meth:`RetryLedger.replay_all`).

A *denial* is not a poison. An authorization refusal is the system working
correctly and must not consume a retry, or a workspace that runs out of
budget would dead-letter its entire rule set. Only unexpected exceptions
count against the retry bound; :func:`default_denial_reason` is the
classifier, and it recognises anything carrying a ``.reason`` attribute —
which is exactly the shape of
:class:`~app_shared.costauth.service.CostAuthorizationDenied`, without this
module having to import it.

Durability
----------
The retry counters and the dead-letter set are **in-process** here. That
is a deliberate, stated limitation: ``refresh_rules`` has no attempt
counter to increment and this task adds no migration. The durability seam
is :class:`RetryLedger`'s ``on_dead_letter`` hook — the scheduler wires it
to a transactional-outbox write through the EXISTING
``create_webhook_event`` consumer (never a new, unregistered task name:
EPA B7's ruling) plus a durable disable of the offending rule, so the
*terminal* decision survives a restart even though the intermediate
attempt count does not. See the PENDING-MIGRATION note in this task's
report for the columns that would make the counter durable too.

Purity
------
Deterministic given its inputs: no clock of its own (every entry point
takes ``now``), no database, no broker, no randomness, no global state.
Framework-free — stdlib only: no SQLAlchemy, celery, scrapy, twisted,
playwright or fastapi anywhere in this module. Only part of that is
machine-checked: ``tests/unit/test_import_boundaries.py``'s
``test_app_shared_does_not_import_scrapy_twisted_playwright`` covers this
module (it imports all of ``app_shared``) for the scraping stack and
fastapi, but there is NO boundary test asserting the stdlib-only claim
for SQLAlchemy or celery — those two are ordinary ``app_shared``
dependencies elsewhere in the package, so importing one here would not
trip any existing test. Treat that half as a convention this docstring
records, not an enforced invariant.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger("app_shared.scheduling.fair_queue")

__all__ = [
    "DEFAULT_DOMAIN_CONCURRENCY",
    "DEFAULT_FLEET_CONCURRENCY",
    "DEFAULT_MAX_ATTEMPTS",
    "WILDCARD_DOMAIN",
    "DeadLetterRecord",
    "Deferral",
    "DeferralReason",
    "FailureDisposition",
    "FailureOutcome",
    "FairShare",
    "FleetLimits",
    "LiveUsage",
    "PassOutcome",
    "PassPlan",
    "RetryLedger",
    "ScheduleCandidate",
    "default_denial_reason",
    "execute_plan",
    "freshness_urgency",
    "normalize_domain",
    "plan_pass",
    "run_pass",
]

#: Simultaneous in-flight fetches one domain may receive from the WHOLE
#: fleet. Small on purpose: this is the number a merchant's WAF sees, and
#: it is shared by every tenant that monitors that merchant.
DEFAULT_DOMAIN_CONCURRENCY = 4

#: Simultaneous in-flight fetches across every domain. The fleet-wide
#: brake — a hundred domains at four each is still four hundred sockets.
DEFAULT_FLEET_CONCURRENCY = 64

#: Consecutive unexpected failures an item may accumulate before it is
#: dead-lettered out of the candidate set.
DEFAULT_MAX_ATTEMPTS = 3

#: Domain sentinel for a candidate whose work spans several domains (a
#: WORKSPACE- or PRODUCT-scope refresh rule, say). The scheduler plane
#: cannot bind a per-domain cap it cannot name, so wildcard candidates are
#: exempt from the per-domain counter — they still consume fleet capacity
#: and a fair-share slot, and their real per-domain accounting happens at
#: dispatch time in C3's per-batch authorization, which does know the
#: domain.
WILDCARD_DOMAIN = "*"


def normalize_domain(domain: str | None) -> str:
    """Lowercase, strip whitespace/trailing dot and a leading ``www.``.

    Two tenants that spell the same merchant ``Merchant.example`` and
    ``www.merchant.example`` are pointed at one host and must share one
    cap; spelling is not a way around the fleet plane. ``None``/empty
    becomes :data:`WILDCARD_DOMAIN`.
    """
    if not domain:
        return WILDCARD_DOMAIN
    value = str(domain).strip().lower().rstrip(".")
    if not value:
        return WILDCARD_DOMAIN
    if value.startswith("www."):
        value = value[4:]
    return value or WILDCARD_DOMAIN


@dataclass(frozen=True)
class ScheduleCandidate:
    """One unit of due work, described completely enough to schedule it.

    ``key`` is a stable identity (the ``refresh_rules.id`` as a string, in
    the scheduler's wiring). It is what the retry ledger counts against
    and what a dead letter is replayed by, so it MUST be stable across
    passes — a per-pass uuid would make bounded retries unbounded.

    ``domain`` is normalized on construction; pass :data:`WILDCARD_DOMAIN`
    (or ``None``) for multi-domain work.

    ``freshness_target_seconds`` is how stale this item is *allowed* to
    get — the cadence, typically. It only feeds :func:`freshness_urgency`,
    i.e. ordering.
    """

    key: str
    workspace_id: str
    domain: str
    due_at: datetime
    priority: int = 0
    last_success_at: datetime | None = None
    freshness_target_seconds: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", normalize_domain(self.domain))
        object.__setattr__(self, "key", str(self.key))
        object.__setattr__(self, "workspace_id", str(self.workspace_id))


@dataclass(frozen=True)
class FleetLimits:
    """The fleet plane's caps. Per-domain first, then the fleet total.

    ``per_domain`` overrides ``default_domain_concurrency`` for named
    domains (a merchant that has told us a number, or one we have burned
    ourselves on). Keys are normalized on lookup, so callers may spell
    them however they like.

    A ``None`` cap means "no ceiling on that plane" — the same posture
    ``cost_budgets``' NULL limits take. It is not the default anywhere.
    """

    default_domain_concurrency: int | None = DEFAULT_DOMAIN_CONCURRENCY
    per_domain: Mapping[str, int] = field(default_factory=dict)
    fleet_concurrency: int | None = DEFAULT_FLEET_CONCURRENCY

    def for_domain(self, domain: str) -> int | None:
        """The cap that applies to ``domain`` (``None`` = uncapped)."""
        normalized = normalize_domain(domain)
        if normalized == WILDCARD_DOMAIN:
            return None
        for candidate_key, value in self.per_domain.items():
            if normalize_domain(candidate_key) == normalized:
                return None if value is None else int(value)
        return (
            None
            if self.default_domain_concurrency is None
            else int(self.default_domain_concurrency)
        )


@dataclass(frozen=True)
class LiveUsage:
    """Occupancy the pass starts from — what is ALREADY in flight.

    Built by the scheduler from C3's live reservations, so the fleet plane
    is measured against the same rows the budget authority holds rather
    than against a private counter this module would have to keep correct
    across restarts.
    """

    per_domain: Mapping[str, int] = field(default_factory=dict)
    total: int = 0

    def domain(self, domain: str) -> int:
        normalized = normalize_domain(domain)
        for key, value in self.per_domain.items():
            if normalize_domain(key) == normalized:
                return int(value)
        return 0


@dataclass(frozen=True)
class FairShare:
    """The tenant plane's policy: relative weights, optional hard share cap.

    ``weights`` maps ``workspace_id`` to a positive relative share; an
    unlisted workspace gets ``default_weight``. A weight of 2 means that
    workspace draws two slots per round where a weight-1 workspace draws
    one — it does NOT mean it may take twice the pass, because rounds stop
    when the pass's batch limit is spent.

    A non-positive weight is treated as "not scheduled this pass"
    (:attr:`DeferralReason.FAIR_SHARE_EXHAUSTED`) rather than as an error:
    zero is how an operator parks a workspace without deleting its rules,
    and it must not be able to wedge the loop that hands out deficits.

    ``max_per_workspace`` is an optional absolute ceiling per pass. It
    defaults to ``None`` because round robin already prevents starvation;
    it exists for the case where an operator wants a hard number.
    """

    weights: Mapping[str, float] = field(default_factory=dict)
    default_weight: float = 1.0
    max_per_workspace: int | None = None

    def weight_for(self, workspace_id: str) -> float:
        raw = self.weights.get(str(workspace_id), self.default_weight)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(self.default_weight)


class DeferralReason(StrEnum):
    """Why a due candidate did not make it into this pass.

    Every one of these is a *deferral*, never a failure: the item keeps
    its unchanged schedule and is a candidate again on the next pass.
    """

    #: The fleet plane's per-domain cap is full — counting every tenant.
    DOMAIN_LIMIT = "DOMAIN_LIMIT"
    #: The fleet-wide concurrency cap is full.
    FLEET_LIMIT = "FLEET_LIMIT"
    #: This pass's batch limit is spent.
    PASS_CAPACITY = "PASS_CAPACITY"
    #: The workspace has drawn its share (weight <= 0, or
    #: ``max_per_workspace``).
    FAIR_SHARE_EXHAUSTED = "FAIR_SHARE_EXHAUSTED"
    #: The item is currently dead-lettered and is not scheduled until it
    #: is replayed.
    DEAD_LETTERED = "DEAD_LETTERED"


@dataclass(frozen=True)
class Deferral:
    """One deferred candidate and the plane that deferred it."""

    candidate: ScheduleCandidate
    reason: DeferralReason


@dataclass(frozen=True)
class PassPlan:
    """The pass's decision: what to attempt, in order, and what was held back."""

    admitted: tuple[ScheduleCandidate, ...] = ()
    deferred: tuple[Deferral, ...] = ()

    def deferrals_for(self, reason: DeferralReason) -> tuple[ScheduleCandidate, ...]:
        return tuple(d.candidate for d in self.deferred if d.reason is reason)

    @property
    def admitted_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.admitted)


def freshness_urgency(candidate: ScheduleCandidate, now: datetime) -> float:
    """How badly this item wants to run, as a unitless non-negative number.

    Two contributions, whichever is larger:

    * **overdue** — how far past ``due_at`` we are, relative to the item's
      freshness target;
    * **staleness** — how long since it last succeeded, relative to the
      same target.

    They differ exactly when a rule has been *deferred* repeatedly: its
    ``due_at`` may have been advanced by a pass that never actually
    dispatched it, while ``last_success_at`` keeps aging. Taking the max
    is what stops a perpetually-deferred item from looking fresh.

    With no target, the result is hours overdue — same shape, coarser
    unit, so mixed candidate sets still order sensibly.

    ORDERING ONLY. Nothing downstream of the sort consults this value; a
    hugely urgent item and a barely-due one clear exactly the same caps
    and exactly the same authorization gate.
    """
    overdue_seconds = max(0.0, (now - candidate.due_at).total_seconds())
    target = candidate.freshness_target_seconds
    if target and int(target) > 0:
        target_seconds = float(int(target))
        since_success = (
            max(0.0, (now - candidate.last_success_at).total_seconds())
            if candidate.last_success_at is not None
            else overdue_seconds
        )
        return max(overdue_seconds / target_seconds, since_success / target_seconds)
    return overdue_seconds / 3600.0


def _ordering_key(candidate: ScheduleCandidate, now: datetime) -> tuple:
    """Deterministic within-workspace order: urgency, then priority, then age.

    ``key`` is the final tiebreak so two candidates that are identical in
    every scheduling respect still order the same way on every run — a
    plan that is not reproducible is a plan that cannot be tested.
    """
    return (
        -freshness_urgency(candidate, now),
        -int(candidate.priority),
        candidate.due_at,
        candidate.key,
    )


def plan_pass(
    candidates: Iterable[ScheduleCandidate],
    *,
    now: datetime,
    batch_limit: int,
    limits: FleetLimits | None = None,
    usage: LiveUsage | None = None,
    share: FairShare | None = None,
    ledger: "RetryLedger | None" = None,
) -> PassPlan:
    """Select this pass's work: fleet caps first, fair share second.

    The algorithm is deficit weighted round robin over per-workspace
    queues, with a per-domain gate applied at the moment an item is popped:

    * a domain-blocked item is deferred and does **not** consume its
      workspace's deficit — being unlucky in *which merchant* you monitor
      must not cost you your turn, or a tenant whose rules cluster on a
      busy domain would be starved by the very mechanism meant to prevent
      starvation;
    * a fleet-cap or batch-limit stop ends the pass, and everything still
      queued is deferred with that reason;
    * dead-lettered keys are filtered out before any of this, so a poison
      item cannot occupy a slot it will only fail in.

    Returns a :class:`PassPlan`; performs no I/O and mutates nothing.
    """
    limits = limits or FleetLimits()
    usage = usage or LiveUsage()
    share = share or FairShare()

    deferred: list[Deferral] = []
    queues: dict[str, deque[ScheduleCandidate]] = {}
    for candidate in candidates:
        if ledger is not None and ledger.is_dead_lettered(candidate.key):
            deferred.append(Deferral(candidate, DeferralReason.DEAD_LETTERED))
            continue
        queues.setdefault(candidate.workspace_id, deque()).append(candidate)

    for workspace_id in list(queues):
        ordered = sorted(queues[workspace_id], key=lambda c: _ordering_key(c, now))
        queues[workspace_id] = deque(ordered)

    # Sorted workspace order, not insertion order: the plan must not
    # depend on which tenant's rows the claim query happened to read first.
    order = sorted(queues)
    deficits: dict[str, float] = {ws: 0.0 for ws in order}
    taken: dict[str, int] = {ws: 0 for ws in order}

    for workspace_id in order:
        if share.weight_for(workspace_id) <= 0:
            while queues[workspace_id]:
                deferred.append(
                    Deferral(
                        queues[workspace_id].popleft(),
                        DeferralReason.FAIR_SHARE_EXHAUSTED,
                    )
                )

    domain_used = {
        normalize_domain(k): int(v) for k, v in dict(usage.per_domain).items()
    }
    total_used = int(usage.total)
    fleet_cap = limits.fleet_concurrency

    admitted: list[ScheduleCandidate] = []
    stop_reason: DeferralReason | None = None
    limit = max(0, int(batch_limit))

    if limit == 0:
        stop_reason = DeferralReason.PASS_CAPACITY

    while stop_reason is None:
        progressed = False
        for workspace_id in order:
            queue = queues[workspace_id]
            if not queue:
                continue
            weight = share.weight_for(workspace_id)
            if weight <= 0:
                continue
            deficits[workspace_id] += weight
            progressed = True
            while queue and deficits[workspace_id] >= 1.0:
                if len(admitted) >= limit:
                    stop_reason = DeferralReason.PASS_CAPACITY
                    break
                if fleet_cap is not None and total_used >= int(fleet_cap):
                    stop_reason = DeferralReason.FLEET_LIMIT
                    break
                if (
                    share.max_per_workspace is not None
                    and taken[workspace_id] >= int(share.max_per_workspace)
                ):
                    while queue:
                        deferred.append(
                            Deferral(
                                queue.popleft(), DeferralReason.FAIR_SHARE_EXHAUSTED
                            )
                        )
                    break

                candidate = queue[0]
                domain_cap = limits.for_domain(candidate.domain)
                used = domain_used.get(candidate.domain, 0)
                if domain_cap is not None and used >= int(domain_cap):
                    # Deferred WITHOUT charging the deficit — see the
                    # docstring: the fleet plane's bad luck is not the
                    # tenant plane's debt.
                    deferred.append(
                        Deferral(queue.popleft(), DeferralReason.DOMAIN_LIMIT)
                    )
                    continue

                queue.popleft()
                admitted.append(candidate)
                deficits[workspace_id] -= 1.0
                taken[workspace_id] += 1
                if candidate.domain != WILDCARD_DOMAIN:
                    domain_used[candidate.domain] = used + 1
                total_used += 1
            if stop_reason is not None:
                break
        if not progressed:
            break

    if stop_reason is not None:
        for workspace_id in order:
            while queues[workspace_id]:
                deferred.append(Deferral(queues[workspace_id].popleft(), stop_reason))

    return PassPlan(admitted=tuple(admitted), deferred=tuple(deferred))


# ---------------------------------------------------------------------------
# Bounded retries, dead-letter, replay
# ---------------------------------------------------------------------------


class FailureDisposition(StrEnum):
    """What the ledger decided to do about one failure."""

    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True)
class DeadLetterRecord:
    """A poison item, parked with everything an operator needs to triage it."""

    key: str
    workspace_id: str
    domain: str
    attempts: int
    last_error: str
    dead_lettered_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureOutcome:
    """The ledger's verdict on one failure."""

    disposition: FailureDisposition
    attempts: int
    record: DeadLetterRecord | None = None


#: Characters of an exception repr kept — a breadcrumb, never a blob.
_MAX_ERROR_CHARS = 500


class RetryLedger:
    """Bounded-retry accounting and the dead-letter set, per item ``key``.

    In-process by construction (see the module docstring). ``on_dead_letter``
    is the durability seam: it is called once, at the moment an item turns
    terminal, with the :class:`DeadLetterRecord`. Exceptions raised by the
    hook are logged and swallowed — a dead-letter sink that can abort the
    pass would recreate the very "one bad item stops everything" failure
    this class exists to remove.

    Not thread-safe, and deliberately so: one scheduler pass runs on one
    thread, and a lock here would imply a sharing model that does not exist.
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        on_dead_letter: Callable[[DeadLetterRecord], None] | None = None,
    ) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._on_dead_letter = on_dead_letter
        self._attempts: dict[str, int] = {}
        self._dead: dict[str, DeadLetterRecord] = {}

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def attempts_for(self, key: str) -> int:
        return int(self._attempts.get(str(key), 0))

    def is_dead_lettered(self, key: str) -> bool:
        return str(key) in self._dead

    def dead_letters(self) -> tuple[DeadLetterRecord, ...]:
        """Every parked item, oldest first — the operator's queue view."""
        return tuple(
            sorted(self._dead.values(), key=lambda r: (r.dead_lettered_at, r.key))
        )

    def record_success(self, key: str) -> None:
        """Clear the attempt count. Consecutive failures are what count.

        A rule that fails, succeeds, and fails again is flaky, not poison,
        and flaky work must not accumulate its way to a dead letter over
        weeks.
        """
        self._attempts.pop(str(key), None)

    def record_failure(
        self,
        candidate: ScheduleCandidate,
        *,
        error: BaseException | str,
        now: datetime,
    ) -> FailureOutcome:
        """Count one unexpected failure; dead-letter at the bound.

        Denials are NOT failures — do not route an authorization refusal
        here (:func:`execute_plan` does not).
        """
        key = str(candidate.key)
        attempts = self.attempts_for(key) + 1
        self._attempts[key] = attempts
        message = (
            error if isinstance(error, str) else repr(error)
        )[:_MAX_ERROR_CHARS]

        if attempts < self._max_attempts:
            logger.warning(
                "fair_queue.item_failed key=%s workspace_id=%s domain=%s "
                "attempt=%d/%d error=%s",
                key,
                candidate.workspace_id,
                candidate.domain,
                attempts,
                self._max_attempts,
                message,
            )
            return FailureOutcome(FailureDisposition.RETRY, attempts)

        record = DeadLetterRecord(
            key=key,
            workspace_id=candidate.workspace_id,
            domain=candidate.domain,
            attempts=attempts,
            last_error=message,
            dead_lettered_at=now,
            payload=dict(candidate.payload or {}),
        )
        self._dead[key] = record
        logger.error(
            "fair_queue.dead_letter key=%s workspace_id=%s domain=%s "
            "attempts=%d error=%s",
            key,
            candidate.workspace_id,
            candidate.domain,
            attempts,
            message,
        )
        if self._on_dead_letter is not None:
            try:
                self._on_dead_letter(record)
            except Exception:
                logger.exception(
                    "fair_queue.dead_letter_sink_failed key=%s — the item is "
                    "still parked in memory; only its durable record is missing",
                    key,
                )
        return FailureOutcome(FailureDisposition.DEAD_LETTER, attempts, record)

    def replay(self, key: str) -> DeadLetterRecord:
        """Un-park one dead letter and reset its attempts. Returns the record.

        Raises ``KeyError`` when nothing is parked under ``key`` — replaying
        something that was never dead-lettered is a caller error, and
        silently succeeding would make an operator believe they had fixed
        something.
        """
        record = self._dead.pop(str(key))
        self._attempts.pop(str(key), None)
        logger.info("fair_queue.replayed key=%s attempts_reset_from=%d", key, record.attempts)
        return record

    def replay_all(self) -> tuple[DeadLetterRecord, ...]:
        """Un-park everything. Returns the records, oldest first."""
        records = self.dead_letters()
        for record in records:
            self._dead.pop(record.key, None)
            self._attempts.pop(record.key, None)
        if records:
            logger.info("fair_queue.replayed_all count=%d", len(records))
        return records


# ---------------------------------------------------------------------------
# Executing a plan
# ---------------------------------------------------------------------------


def default_denial_reason(exc: BaseException) -> str | None:
    """Classify ``exc`` as an authorization DENIAL (a reason) or a failure.

    Duck-typed on ``.reason`` so this module never imports C3:
    :class:`~app_shared.costauth.service.CostAuthorizationDenied` carries a
    :class:`~app_shared.costauth.service.DenialReason`, and anything else
    that models a refusal the same way is recognised for free.

    Returning ``None`` means "this was not a refusal, it was a fault" and
    sends the item to the retry ledger.
    """
    reason = getattr(exc, "reason", None)
    if reason is None:
        return None
    return str(getattr(reason, "value", reason))


@dataclass(frozen=True)
class PassOutcome:
    """What actually happened when a plan was executed."""

    plan: PassPlan
    dispatched: tuple[str, ...] = ()
    denied: tuple[tuple[str, str], ...] = ()
    retried: tuple[str, ...] = ()
    dead_lettered: tuple[str, ...] = ()

    @property
    def denied_keys(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.denied)


def execute_plan(
    plan: PassPlan,
    *,
    now: datetime,
    gate: Callable[[ScheduleCandidate], Any],
    dispatch: Callable[[ScheduleCandidate, Any], None],
    ledger: RetryLedger,
    denial_reason: Callable[[BaseException], str | None] = default_denial_reason,
) -> PassOutcome:
    """Authorize then dispatch each admitted candidate, isolating failures.

    ``gate`` is the authorization seam — in production, a call into C3
    (:class:`~app_shared.costauth.service.CostAuthorizationService`). It is
    the ONLY thing that may permit spending, it is consulted for **every**
    admitted candidate, and nothing in the planning above can cause it to
    be skipped. Whatever it returns is handed to ``dispatch`` as the grant.

    Three outcomes per candidate:

    * ``gate`` raises something ``denial_reason`` recognises -> **denied**.
      Not dispatched, not retried, not counted against the retry bound: a
      refusal is the system working.
    * ``gate`` or ``dispatch`` raises anything else -> **fault**, routed to
      ``ledger`` for a bounded retry or a dead letter. The pass continues
      with the next candidate; one poison item can no longer stop every
      other tenant's scheduling.
    * neither raises -> **dispatched**, and the item's consecutive-failure
      count is cleared.
    """
    dispatched: list[str] = []
    denied: list[tuple[str, str]] = []
    retried: list[str] = []
    dead: list[str] = []

    for candidate in plan.admitted:
        try:
            grant = gate(candidate)
        except Exception as exc:  # noqa: BLE001 - classified below, never re-raised
            reason = denial_reason(exc)
            if reason is not None:
                denied.append((candidate.key, reason))
                logger.info(
                    "fair_queue.denied key=%s workspace_id=%s domain=%s reason=%s",
                    candidate.key,
                    candidate.workspace_id,
                    candidate.domain,
                    reason,
                )
                continue
            outcome = ledger.record_failure(candidate, error=exc, now=now)
            (dead if outcome.disposition is FailureDisposition.DEAD_LETTER else retried).append(
                candidate.key
            )
            continue

        try:
            dispatch(candidate, grant)
        except Exception as exc:  # noqa: BLE001 - isolated, never re-raised
            outcome = ledger.record_failure(candidate, error=exc, now=now)
            (dead if outcome.disposition is FailureDisposition.DEAD_LETTER else retried).append(
                candidate.key
            )
            continue

        ledger.record_success(candidate.key)
        dispatched.append(candidate.key)

    return PassOutcome(
        plan=plan,
        dispatched=tuple(dispatched),
        denied=tuple(denied),
        retried=tuple(retried),
        dead_lettered=tuple(dead),
    )


def run_pass(
    candidates: Sequence[ScheduleCandidate],
    *,
    now: datetime,
    batch_limit: int,
    gate: Callable[[ScheduleCandidate], Any],
    dispatch: Callable[[ScheduleCandidate, Any], None],
    ledger: RetryLedger,
    limits: FleetLimits | None = None,
    usage: LiveUsage | None = None,
    share: FairShare | None = None,
    denial_reason: Callable[[BaseException], str | None] = default_denial_reason,
) -> PassOutcome:
    """:func:`plan_pass` then :func:`execute_plan` — the whole pass, one call."""
    plan = plan_pass(
        candidates,
        now=now,
        batch_limit=batch_limit,
        limits=limits,
        usage=usage,
        share=share,
        ledger=ledger,
    )
    return execute_plan(
        plan,
        now=now,
        gate=gate,
        dispatch=dispatch,
        ledger=ledger,
        denial_reason=denial_reason,
    )
