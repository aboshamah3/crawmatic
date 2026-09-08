"""``CostAuthorizationService`` — the single gate every paid dispatch passes.

Before C3 the engine had **five** independent, partially-overlapping cost
brakes (a Redis monthly counter, three Redis rate ceilings, a domain
cooldown, the durable proxy breaker, and per-job requeue caps) and no
single place that could answer "may this workspace spend this money on
this domain right now". Each brake was individually reasonable and
collectively unauthoritative: none of them reserved anything, so two
concurrent decisions could both observe room for the last micro-USD, and none
of them could deny work on a domain nobody had certified.

This module is that single place. Its contract, in one sentence: **one
database transaction checks every gate and reserves every dimension, or
raises.**

The authoritative store is PostgreSQL
--------------------------------------
Budget counters and reservations live in ``cost_budgets`` /
``fleet_cost_budgets`` / ``cost_reservations`` and every check-and-reserve
takes ``SELECT ... FOR UPDATE`` on the budget rows. There is deliberately
**no Redis/DB dual write for correctness**: two stores that must agree are
two stores that will eventually disagree, and the incident that blinds one
is exactly the incident during which the ceiling matters. Redis may cache
*denials* for fast-path rejection — a cached denial that is stale merely
costs an unnecessary round trip — but it never grants.

The gates, in the order they are evaluated
-------------------------------------------
Order matters only where two gates could both deny; the earlier one wins
and its reason is the one reported. It is arranged cheapest-and-most-
fundamental first:

1. **Workspace entitlement**, from durable local evidence
   (``workspace_entitlements``). No row, stale evidence, or a non-ACTIVE
   state all deny with ``ENTITLEMENT_INACTIVE``. Deliberately independent
   of the SaaS being reachable — see the model module's "Entitlement
   evidence" section for why the engine grew its own table.
2. **Breaker state** (``proxy_circuit_breakers``). Missing row or
   ``evaluated_at`` older than the configured maximum evidence age denies
   with ``BREAKER_EVIDENCE_STALE``; an OPEN breaker denies with
   ``BREAKER_OPEN``. **Fail-closed**: absent evidence is not permission.
   The breaker is consulted for every transport, DIRECT included — its
   freshness is evidence that the fleet's cost brake is running at all,
   and a fleet whose brake has stopped reporting has no business starting
   new work of any kind.
3. **Domain state**, via C2 (:func:`app_shared.domains.state_lookup.
   get_domain_state` + :func:`~app_shared.domains.state_lookup.
   authorization_rules_for_state`). See "Reconciling C2's nested rules
   with DOMAIN_NOT_CERTIFIED" below.
4. **Concurrency cap** — live (unexpired) ``RESERVED`` grants for the
   scope, against ``max_concurrent_reservations``.
5. **Dedupe / coalescing** — a live grant carrying the same
   ``dedupe_key`` is returned as-is rather than re-reserved.
6. **The four budget dimensions**, tenant first then fleet, money then
   bytes then requests then browser-seconds.

Reconciling C2's nested rules with ``DOMAIN_NOT_CERTIFIED``
------------------------------------------------------------
C2 (``app_shared.domains.state_lookup``) deliberately models domain
authorization as **three nested gates** — ``paid_allowed`` >=
``broad_crawl_allowed`` >= ``expensive_escalation_allowed`` — rather than
as one boolean, and its docstring is explicit that ``UNKNOWN`` means
"paid work is allowed, broad crawl is not; only a tiny DIRECT canary is
permitted". The C3 contract, meanwhile, names exactly one denial reason,
``DOMAIN_NOT_CERTIFIED``, for the ``UNKNOWN``-domain case.

Those are not in conflict, but flattening C2's three gates onto one
reason would lose information a denial needs to carry (a quarantined
domain and an uncertified one demand completely different operator
responses). **Resolution, and it is C3's to make since C3 is the only
consumer:** the three gates map one-to-one onto three denial reasons, and
``DOMAIN_NOT_CERTIFIED`` keeps precisely the meaning the contract's test
asserts —

* ``paid_allowed`` False (``QUARANTINED``/``UNSUPPORTED``) ->
  :attr:`DenialReason.DOMAIN_QUARANTINED`;
* ``broad_crawl_allowed`` False (``UNKNOWN``) ->
  :attr:`DenialReason.DOMAIN_NOT_CERTIFIED` — the contract's reason,
  raised for exactly the contract's case;
* ``expensive_escalation_allowed`` False (``DEGRADED``) ->
  :attr:`DenialReason.DOMAIN_ESCALATION_DENIED`.

Which gate a request must clear is decided by
:func:`_required_domain_gate` from its purpose and transport, and the
"tiny DIRECT canary" C2 carves out of ``UNKNOWN`` is given a concrete
definition here (a ``DISCOVERY`` request on ``DIRECT`` transport) rather
than left as prose. Every other purpose is broad crawl, so an uncertified
domain cannot be refreshed, retried or escalated into — which is the
behaviour the contract's test pins.

Leases, not bare TTLs
---------------------
A reservation's ``lease_expires_at`` is renewed by :meth:`~Cost
AuthorizationService.heartbeat` while its operation is live. When it
lapses, :func:`sweep_expired_reservations` does **not** simply release
it: it first asks C1's ledger whether an operation opened under that
``authorization_id`` is still open (``network_operations`` with
``closed_at IS NULL``). If one is, the money stays reserved and the lease
is extended. An expired lease is evidence that a *heartbeat* stopped —
which happens whenever a worker is paused, throttled, or slow — and
treating that as evidence that the *work* stopped is precisely how a
budget gets spent twice while its counters look healthy.

One grant, MANY operations — the cardinality, stated
-----------------------------------------------------
The C3 plan wrote its invariant as "every authorization grant has exactly
one opened operation". **That is not the shape this engine dispatches**,
and pretending otherwise is what EPA Phase C's gate review found: an HTTP
batch is authorized ONCE for up to ``SCRAPE_DISPATCH_HTTP_BATCH_MAX``
targets, priced for the whole batch, and the spider then opens one
physical operation per target under that single ``authorization_id``. A
discovery run does the same across its access ladder. So the resolution,
recorded here as the deliberate deviation it is:

    **A grant covers one BATCH of work: one or many operations. The
    network boundary NEVER terminates a grant — each operation's close
    accrues its OWN observed cost through the non-terminal**
    :meth:`~CostAuthorizationService.settle_partial`. **Only the site
    that MINTED the grant may terminate it: explicitly, through
    :meth:`~CostAuthorizationService.settle` / :meth:`~Cost
    AuthorizationService.release`, when it can observe the work's end
    (a discovery run in this process); or implicitly, through lease
    expiry +** :func:`sweep_expired_reservations` **, when it cannot (a
    batch POSTed to a remote Scrapyd node). The sweeper's ledger-liveness
    check is what makes the implicit close safe: while ANY operation is
    open under the grant the lease is extended, never reaped.**

The consequence, which is the property the whole fix exists for: a
grant's settled totals are the SUM of its operations' observed costs, and
its residual hold (``reserved − accrued``, floored at zero) is returned
exactly once, at the terminal transition. Before this, the FIRST
operation's close settled the entire grant with one operation's cost and
handed the rest of the hold back to the budget — twenty targets dispatched
under a twenty-unit reservation settled as one unit and released
nineteen, so the ceiling stopped binding after the first fetch.

Settlement and release are compare-and-set on the reservation's own
state, so both are idempotent: a replayed settle finds a terminal row and
changes no counter. :meth:`~CostAuthorizationService.settle_partial` is
*accumulating* rather than compare-and-set, so its idempotence comes from
its ONE caller instead: C4's recorder accrues only when its own
``closed_at IS NULL`` update actually closed the ledger row, so a
redelivered or replayed close accrues nothing a second time.

Warning events go through an EXISTING consumer
-----------------------------------------------
Crossing 50/75/90% of a limit writes an outbox message whose
``task_name`` is :data:`~app_shared.task_names.CREATE_WEBHOOK_EVENT` —
the already-registered ``webhook_events`` consumer
(``apps/workers/app/workers/tasks_webhooks.py``) — carrying
``event_type = budget.threshold.warning``. No new task name is invented,
because an outbox row naming a task nothing consumes is a message that
looks delivered and never is (EPA B7's ruling). Each threshold fires at
most once per budget row per period; the crossings already announced live
on the budget row (``warned_thresholds``), not in the outbox, so a drain
cannot cause a re-emit and a replay cannot cause a duplicate.

Seams
-----
``authorize``/``heartbeat``/``settle``/``release`` run on the **tenant
seam** (``crawmatic_app`` under ``SET LOCAL app.workspace_id``): each
touches exactly one workspace's rows, so forced RLS is a real guard.
:func:`sweep_expired_reservations` runs on the sanctioned **BYPASSRLS
system seam** (``get_system_session``) because it is inherently
cross-tenant and must additionally read ``network_operations``, which has
no workspace column at all — the same seam, for the same reason, as C1's
allocation writes and the scheduler's due-rule claim.

Cross-workspace coalescing is NOT supported here (W4.3 owns it). A
request names exactly one workspace; handing this service an allocation
set raises :class:`CrossWorkspaceCoalescingUnsupported` rather than
guessing a split, because "a shared fetch requires a grant per
participating workspace, settled deterministically" is a decision W4.3
has not made yet and inventing one here would be inventing money.

Framework-free: SQLAlchemy + stdlib + ``app_shared`` only. No celery, no
scrapy, no fastapi (``tests/unit/test_import_boundaries.py``).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Iterator, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app_shared.domains.state_lookup import (
    authorization_rules_for_state,
    get_domain_state,
)
from app_shared.enums import WebhookEventType
from app_shared.ids import new_uuid7
from app_shared.models.cost_authorization import (
    BUDGET_WARNING_THRESHOLDS,
    AuthorizationPurpose,
    CostBudget,
    CostReservation,
    EntitlementState,
    FleetCostBudget,
    ReservationState,
    WorkspaceEntitlement,
)
from app_shared.models.network_operations import NetworkOperation
from app_shared.models.proxy_breaker import (
    GLOBAL_BREAKER_SCOPE,
    ProxyBreakerState,
    ProxyCircuitBreaker,
)
from app_shared.outbox.writer import write_outbox_message
from app_shared.task_names import CREATE_WEBHOOK_EVENT

logger = logging.getLogger(__name__)

__all__ = [
    "BUDGET_WARNING_EVENT_TYPE",
    "DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS",
    "DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS",
    "DEFAULT_ESTIMATED_BYTES_PER_REQUEST",
    "DEFAULT_LEASE_SECONDS",
    "FLEET_PROVIDER_BROWSER",
    "FLEET_PROVIDER_DIRECT",
    "FLEET_PROVIDER_PROXY",
    "AuthorizationGrant",
    "AuthorizationRequest",
    "CostAuthorizationDenied",
    "CostAuthorizationError",
    "CostAuthorizationService",
    "CrossWorkspaceCoalescingUnsupported",
    "DenialReason",
    "MICRO_UNITS_PER_USD",
    "SettledCost",
    "authorize_or_none",
    "estimate_bytes",
    "period_key_for",
    "release_reservations_for_scrape_job",
    "sweep_expired_reservations",
]

#: How long a fresh grant's lease runs before the sweeper may consider it
#: (subject to the ledger check). Long enough that an ordinary slow fetch
#: never needs a heartbeat; short enough that a crashed worker's money is
#: back inside one maintenance cycle.
DEFAULT_LEASE_SECONDS = 900

#: Breaker evidence older than this is treated as MISSING -> DENY. The
#: breaker's own evaluator lease (``app_shared.access.breaker``) re-runs
#: far more often than this, so a stale row means the evaluator itself
#: has stopped — which is exactly the condition under which no new paid
#: work should start.
DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS = 3600

#: Entitlement evidence older than this is treated as INACTIVE (W1.1's
#: "staleness treated as inactive"). Generous relative to the SaaS's
#: billing-event replication cadence, because the cost of a false denial
#: is a paused scrape and the cost of a false grant is unbilled spend on
#: a cancelled account.
DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS = 86_400

#: ``webhook_events.event_type`` for a budget-threshold crossing. Taken
#: from the shared taxonomy rather than spelled again here, so the
#: producer and any subscriber cannot drift: it is a free string on a
#: ``String(64)`` column, exactly like every other ``WebhookEventType``
#: value (see that enum's docstring), and endpoint subscriptions stay
#: forward-compatible with types they do not know.
BUDGET_WARNING_EVENT_TYPE = WebhookEventType.BUDGET_THRESHOLD_WARNING.value

#: Fleet budget ``scope_key`` values used by the dispatch call sites.
#:
#: The planner knows the TRANSPORT CLASS a batch is bound for; it does
#: not yet know which provider *account* the spider will end up resolving
#: (that is decided per-target inside the scrape path, from the
#: workspace's ``proxy_providers`` rows). Scoping the fleet budget by
#: transport class is therefore the most specific true statement the
#: authorization site can make, and ``scope_key`` is deliberately free
#: text — the same "granularity without a schema change" trick
#: ``proxy_circuit_breakers.scope_key`` uses — so a per-account budget can
#: refine it later with no migration and no change to this contract.
FLEET_PROVIDER_PROXY = "proxy"
FLEET_PROVIDER_BROWSER = "browser"
FLEET_PROVIDER_DIRECT = "direct"

#: Ledger units in one US dollar, and the ONLY definition of the ledger's
#: money unit in this repository (H4/B1, 2026-09-03).
#:
#: Every ``*_cost_micro_units`` column, every JSON key that carries the
#: name, and :data:`app_shared.costauth.fleet_budget_policy.USD_TO_UNITS`
#: are this unit. It used to be cents, and cents were the bug: the
#: cheapest real request this fleet makes costs ``$0.0000046``, so at the
#: cents scale EVERY request — a $0.0000046 direct fetch and a $0.00021
#: amazon.sa proxied fetch alike — floored to the same ``1``, booking
#: spend 47x to 2174x over reality and making the ledger's own numbers
#: useless as evidence. A micro-USD represents both exactly, and
#: ``bigint`` holds a fleet's spend for longer than the fleet will exist.
MICRO_UNITS_PER_USD = 1_000_000

#: Bytes assumed for one request when the caller has no better figure.
#: A competitor product page plus the subresources a fetch pulls in,
#: rounded to a memorable number. It is an ESTIMATE and is expected to be
#: wrong — settlement replaces it with the transport-observed count — but
#: it must be non-trivial, because a byte budget reserved at zero is a
#: byte budget that never binds.
DEFAULT_ESTIMATED_BYTES_PER_REQUEST = 250_000

#: Release reasons recorded on ``cost_reservations.release_reason``.
RELEASE_REASON_FAILED_BEFORE_DISPATCH = "FAILED_BEFORE_DISPATCH"
RELEASE_REASON_CANCELLED_JOB = "CANCELLED_JOB"
RELEASE_REASON_LEASE_EXPIRED = "LEASE_EXPIRED"


class DenialReason(StrEnum):
    """Why an authorization was refused.

    A ``StrEnum``, so ``e.value.reason == "BYTE_BUDGET_EXCEEDED"`` is true
    of the member itself and callers may compare against either the member
    or its string without a conversion step.
    """

    #: No entitlement row, stale evidence, or a non-ACTIVE state — all
    #: three collapse here because all three mean the same thing to a
    #: dispatch: this workspace is not currently entitled to paid work.
    ENTITLEMENT_INACTIVE = "ENTITLEMENT_INACTIVE"
    #: No breaker row at all, or ``evaluated_at`` too old. Fail-closed.
    BREAKER_EVIDENCE_STALE = "BREAKER_EVIDENCE_STALE"
    #: The breaker is durably OPEN.
    BREAKER_OPEN = "BREAKER_OPEN"
    #: C2 ``paid_allowed`` False — ``QUARANTINED``/``UNSUPPORTED``.
    DOMAIN_QUARANTINED = "DOMAIN_QUARANTINED"
    #: C2 ``broad_crawl_allowed`` False — ``UNKNOWN``. The contract's
    #: reason for an uncertified domain.
    DOMAIN_NOT_CERTIFIED = "DOMAIN_NOT_CERTIFIED"
    #: C2 ``expensive_escalation_allowed`` False — ``DEGRADED``.
    DOMAIN_ESCALATION_DENIED = "DOMAIN_ESCALATION_DENIED"
    #: Too many simultaneously-live grants for the scope.
    CONCURRENCY_CAP_EXCEEDED = "CONCURRENCY_CAP_EXCEEDED"
    #: The four dimensions. The same reason is used whether the tenant or
    #: the fleet budget was the binding one; which scope bound it is on
    #: the exception's ``detail``.
    MONEY_BUDGET_EXCEEDED = "MONEY_BUDGET_EXCEEDED"
    BYTE_BUDGET_EXCEEDED = "BYTE_BUDGET_EXCEEDED"
    REQUEST_BUDGET_EXCEEDED = "REQUEST_BUDGET_EXCEEDED"
    BROWSER_SECOND_BUDGET_EXCEEDED = "BROWSER_SECOND_BUDGET_EXCEEDED"


class CostAuthorizationError(Exception):
    """Base class for this module's errors."""


class CostAuthorizationDenied(CostAuthorizationError):
    """The request was refused. ``reason`` is a :class:`DenialReason`.

    ``detail`` carries the human-readable specifics (which scope bound,
    what the domain state was, how stale the evidence was). Callers branch
    on ``reason`` and log ``detail``; nothing should ever parse ``detail``.
    """

    def __init__(self, reason: DenialReason | str, detail: str = "") -> None:
        self.reason = DenialReason(reason)
        self.detail = detail
        super().__init__(f"{self.reason.value}: {detail}" if detail else self.reason.value)


class CrossWorkspaceCoalescingUnsupported(CostAuthorizationError):
    """A request named more than one workspace. Not supported until W4.3.

    Raised rather than guessed. A coalesced fetch shared by two workspaces
    needs a grant per participating workspace settled by a *deterministic*
    split, and the rule for that split is W4.3's decision — picking one
    here would be inventing an allocation the billing side never agreed
    to.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationRequest:
    """One paid dispatch, described completely enough to decide it.

    ``workspace_id`` is a SINGLE workspace, by construction: passing a
    collection raises :class:`CrossWorkspaceCoalescingUnsupported` (W4.3).

    ``estimated_*`` are the four reserved dimensions. They are estimates
    and are expected to be wrong — that is what settlement is for — but
    they must be non-negative integers, because money is never a float
    (§19, the same posture as
    :func:`app_shared.models.network_operations.allocate_cost_largest_remainder`).

    ``dedupe_key`` is optional. When supplied, a live grant already
    carrying it is returned instead of a second reservation, which is what
    makes an at-least-once Celery redelivery cost nothing.
    """

    workspace_id: uuid.UUID
    domain: str
    transport: str
    provider: str
    estimated_bytes: int
    estimated_cost_micro_units: int
    purpose: AuthorizationPurpose
    estimated_requests: int = 1
    estimated_browser_seconds: int = 0
    currency: str = "USD"
    scrape_job_id: uuid.UUID | None = None
    dedupe_key: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workspace_id, (list, tuple, set, frozenset, dict)):
            raise CrossWorkspaceCoalescingUnsupported(
                "AuthorizationRequest names exactly one workspace; cross-workspace "
                "coalescing (a grant per participating workspace, settled by a "
                "deterministic split) is W4.3's decision and is not implemented here"
            )
        for name in (
            "estimated_bytes",
            "estimated_cost_micro_units",
            "estimated_requests",
            "estimated_browser_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{name} must be an int, got {type(value)!r} — a reserved "
                    "quantity is a counted integer, never a float"
                )
            if value < 0:
                raise ValueError(f"{name} must be non-negative: {value!r}")


@dataclass(frozen=True)
class AuthorizationGrant:
    """Proof that a reservation was taken. Returned by ``authorize``.

    ``authorization_id`` is what C4 stamps onto
    ``network_operations.authorization_id``, and what ``heartbeat`` /
    ``settle`` / ``release`` address the reservation by.

    ``replayed`` marks a grant returned by the dedupe path rather than
    freshly reserved — the caller should treat it exactly the same way
    (it is a valid grant), but it is surfaced so a caller that also
    dedupes can tell the two apart in its logs.

    ``entitlement_version`` and ``breaker_decision`` are the OTHER two
    decision facts this grant was issued on, carried out to the caller
    for exactly one reason: C1's ``network_operations`` has a column for
    each (``entitlement_version``, ``budget_decision_version``,
    ``breaker_decision``) and nothing was populating them, so every
    operation row recorded WHICH grant it ran under but not WHAT that
    grant decided. They are additive fields with safe defaults, so a
    caller that predates them is unaffected.
    """

    authorization_id: uuid.UUID
    workspace_id: uuid.UUID
    budget_decision_version: str
    lease_expires_at: datetime
    reserved_cost_micro_units: int
    reserved_bytes: int
    reserved_requests: int
    reserved_browser_seconds: int
    currency: str
    replayed: bool = False
    #: The ``workspace_entitlements.evidence_version`` this grant cleared
    #: the entitlement gate on. ``None`` only for a legacy row written
    #: before the column existed.
    entitlement_version: str | None = None
    #: The breaker verdict at authorize time (``CLOSED`` — an OPEN or
    #: stale breaker denies, so a grant only ever carries a passing one).
    breaker_decision: str | None = None


@dataclass(frozen=True)
class SettledCost:
    """What the operation ACTUALLY consumed, across the same four dimensions.

    Defaults exist because most callers know their money and their bytes
    and nothing else; an omitted dimension settles to zero, which returns
    that dimension's whole reservation to the budget.
    """

    cost_micro_units: int
    bytes_used: int = 0
    requests: int = 1
    browser_seconds: int = 0

    def __post_init__(self) -> None:
        for name in ("cost_micro_units", "bytes_used", "requests", "browser_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, got {type(value)!r}")
            if value < 0:
                raise ValueError(f"{name} must be non-negative: {value!r}")


@dataclass(frozen=True)
class _Dimension:
    """One budget dimension: its columns, its denial reason, its request field.

    Declaring the four dimensions as data rather than as four copies of
    the same ``if`` is what makes "all dimensions checked in one
    transaction" auditable — the check loop and the reserve loop walk the
    same tuple, so a dimension cannot be enforced in one and forgotten in
    the other.
    """

    name: str
    limit_column: str
    reserved_column: str
    settled_column: str
    reason: DenialReason


#: Money first: it is the dimension a human budget is actually expressed
#: in, so when several are exceeded at once it is the most useful reason
#: to report. The rest follow in decreasing generality.
_DIMENSIONS: tuple[_Dimension, ...] = (
    _Dimension(
        "cost_micro_units",
        "limit_cost_micro_units",
        "reserved_cost_micro_units",
        "settled_cost_micro_units",
        DenialReason.MONEY_BUDGET_EXCEEDED,
    ),
    _Dimension(
        "bytes",
        "limit_bytes",
        "reserved_bytes",
        "settled_bytes",
        DenialReason.BYTE_BUDGET_EXCEEDED,
    ),
    _Dimension(
        "requests",
        "limit_requests",
        "reserved_requests",
        "settled_requests",
        DenialReason.REQUEST_BUDGET_EXCEEDED,
    ),
    _Dimension(
        "browser_seconds",
        "limit_browser_seconds",
        "reserved_browser_seconds",
        "settled_browser_seconds",
        DenialReason.BROWSER_SECOND_BUDGET_EXCEEDED,
    ),
)


class _DomainGate(StrEnum):
    """Which of C2's three nested gates a request must clear."""

    PAID = "PAID"
    BROAD_CRAWL = "BROAD_CRAWL"
    EXPENSIVE_ESCALATION = "EXPENSIVE_ESCALATION"


def _required_domain_gate(purpose: AuthorizationPurpose, transport: str) -> _DomainGate:
    """The C2 gate this request must clear — the concrete reading of C2's prose.

    C2 states that ``UNKNOWN`` permits "only a tiny DIRECT canary" and
    that ``DEGRADED`` denies "expensive escalation". Neither phrase is
    self-executing, so C3 (their only consumer) gives each one exactly one
    meaning:

    * **expensive escalation** — a ``BROWSER_ESCALATION`` purpose, or any
      request on ``BROWSER`` transport. A real browser navigation is the
      expensive path by definition; nothing else in the fleet costs
      browser-seconds.
    * **tiny DIRECT canary** — a ``DISCOVERY`` request on ``DIRECT``
      transport. Discovery is the only purpose whose job is to *learn*
      whether a domain works, and DIRECT is the only transport that costs
      no provider money; together they are the one shape of work that must
      remain possible on an uncertified domain, or no domain could ever
      leave ``UNKNOWN``.
    * **everything else** is broad crawl, so an uncertified domain cannot
      be refreshed, retried, fallen back to, or manually rechecked.
    """
    if purpose is AuthorizationPurpose.BROWSER_ESCALATION or str(transport).upper() == "BROWSER":
        return _DomainGate.EXPENSIVE_ESCALATION
    if purpose is AuthorizationPurpose.DISCOVERY and str(transport).upper() == "DIRECT":
        return _DomainGate.PAID
    return _DomainGate.BROAD_CRAWL


def period_key_for(moment: datetime) -> str:
    """The budget period ``moment`` falls in — ``%Y_%m``.

    Deliberately the SAME spelling as
    ``app_shared.access.budget._monthly_budget_key``'s month suffix, so
    the durable counter and the Redis counter it supersedes describe the
    same window and can be compared during the cutover instead of
    disagreeing by a day.
    """
    return f"{moment:%Y_%m}"


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

SessionScope = Callable[[], AbstractContextManager[Session]]


class CostAuthorizationService:
    """The one gate. See the module docstring for the full contract.

    Args:
        session_scope: zero-argument callable returning a context manager
            that yields a :class:`Session` on the **tenant** seam.
            Defaults to :func:`app_shared.database.get_session`, imported
            lazily so this module stays importable (and unit-testable)
            without any DSN configured.
        system_session_scope: the same, on the sanctioned BYPASSRLS
            system seam. Used ONLY by the lease sweeper. Defaults to
            :func:`app_shared.database.get_system_session`.
        now: injectable clock returning an aware UTC datetime (tests).
        default_workspace_id: convenience for callers that operate on one
            workspace for their whole lifetime (and for
            :meth:`remaining_budget_micro_units`).
        lease_seconds / breaker_max_evidence_age_seconds /
        entitlement_max_evidence_age_seconds: the three durations the
            contract makes decisions from. Constructor arguments rather
            than settings so a caller with a genuinely different risk
            posture (the API's synchronous manual recheck, say) can state
            it locally instead of moving a global.
    """

    def __init__(
        self,
        session_scope: SessionScope | None = None,
        *,
        system_session_scope: SessionScope | None = None,
        now: Callable[[], datetime] | None = None,
        default_workspace_id: uuid.UUID | str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        breaker_max_evidence_age_seconds: int = DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS,
        entitlement_max_evidence_age_seconds: int = (
            DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS
        ),
        breaker_scope_key: str = GLOBAL_BREAKER_SCOPE,
        fleet_snapshot_reader: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._session_scope = session_scope
        self._system_session_scope = system_session_scope
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._default_workspace_id = (
            _as_uuid(default_workspace_id) if default_workspace_id is not None else None
        )
        self._lease_seconds = int(lease_seconds)
        self._breaker_max_evidence_age_seconds = int(breaker_max_evidence_age_seconds)
        self._entitlement_max_evidence_age_seconds = int(
            entitlement_max_evidence_age_seconds
        )
        self._breaker_scope_key = breaker_scope_key
        #: EPA B5/F10. Optional ``(domain, transport) -> FleetSnapshot``
        #: callable used ONLY to enrich a concurrency denial's message
        #: (see :meth:`_check_concurrency`). ``None`` — the default —
        #: leaves every denial message exactly as it was; nothing in this
        #: service ever branches on what it returns, because fleet
        #: admission is the Redis lease at the request boundary, not a
        #: decision taken here.
        self._fleet_snapshot_reader = fleet_snapshot_reader

    # -- session plumbing ---------------------------------------------------

    @contextmanager
    def _tenant_session(self, workspace_id: uuid.UUID) -> Iterator[Session]:
        """One transaction on the tenant seam, scoped to ``workspace_id``.

        ``set_workspace_context`` issues ``set_config('app.workspace_id',
        ..., true)`` — transaction-local, safe under PgBouncer transaction
        pooling — so the GUC is set INSIDE the transaction the caller then
        commits, and dies with it.
        """
        scope = self._session_scope
        if scope is None:
            from app_shared.database import get_session

            scope = get_session
        from app_shared.database import set_workspace_context

        with scope() as session:
            set_workspace_context(session, workspace_id)
            yield session

    @contextmanager
    def _system_session(self) -> Iterator[Session]:
        """One transaction on the sanctioned BYPASSRLS system seam."""
        scope = self._system_session_scope
        if scope is None:
            from app_shared.database import get_system_session

            scope = get_system_session
        with scope() as session:
            yield session

    # -- the contract -------------------------------------------------------

    def authorize(self, req: AuthorizationRequest) -> AuthorizationGrant:
        """Check every gate and reserve every dimension in ONE transaction.

        Returns an :class:`AuthorizationGrant` carrying
        ``authorization_id`` + ``budget_decision_version``, or raises
        :class:`CostAuthorizationDenied` with a :class:`DenialReason`.

        Nothing is reserved on any denial path: the reservation INSERT and
        the counter increments are the last things the transaction does,
        after every gate has passed, so a denial cannot leave a partial
        hold behind.
        """
        workspace_id = _as_uuid(req.workspace_id)
        now = self._now()
        period = period_key_for(now)

        with self._tenant_session(workspace_id) as session:
            # 5. Dedupe FIRST among the DB reads: a redelivery must cost
            #    one indexed lookup, not a full gate evaluation. It is
            #    listed fifth in the contract's ordering because it is the
            #    fifth thing that can *deny*; it is executed first because
            #    when it hits, nothing else needs deciding.
            #
            #    Two SIMULTANEOUS first-deliveries of the same key both
            #    find nothing here and both insert; the partial unique
            #    index (`uq_cost_reservations_live_dedupe_key`) then
            #    rejects the loser at COMMIT with an IntegrityError, which
            #    propagates rather than becoming a denial. That is the
            #    correct outcome and not a hole: the index is what
            #    guarantees at most one LIVE grant per key, the loser
            #    reserved nothing (its whole transaction rolls back), and
            #    an error is what a redelivery-safe caller retries on —
            #    where a silent "denied" would look like a budget refusal
            #    and be logged as one.
            if req.dedupe_key is not None:
                existing = session.execute(
                    select(CostReservation)
                    .where(
                        CostReservation.workspace_id == workspace_id,
                        CostReservation.dedupe_key == req.dedupe_key,
                        CostReservation.state == ReservationState.RESERVED,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    existing.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                    # Build the grant BEFORE the commit: `expire_on_commit`
                    # is False on this repository's sessionmaker, but a
                    # caller-supplied factory need not be, and a grant that
                    # depends on that setting is a grant that breaks in
                    # somebody else's process.
                    grant = _grant_from(existing, replayed=True)
                    session.commit()
                    return grant

            entitlement_version = self._check_entitlement(session, workspace_id, now)
            breaker_decision = self._check_breaker(session, now)
            self._check_domain(session, req)

            # 7. Lock the budget rows. FLEET first, then TENANT — always,
            #    everywhere in this module. A fixed global lock order is
            #    the only thing standing between "two budgets per decision"
            #    and a deadlock storm the first time two workspaces
            #    authorize against the same provider concurrently.
            fleet_budget = _lock_fleet_budget(session, req.provider, period, req.currency)
            tenant_budget = _lock_tenant_budget(session, workspace_id, period, req.currency)

            self._check_concurrency(session, workspace_id, tenant_budget, now, req=req)

            wanted = {
                "cost_micro_units": req.estimated_cost_micro_units,
                "bytes": req.estimated_bytes,
                "requests": req.estimated_requests,
                "browser_seconds": req.estimated_browser_seconds,
            }
            # 6. ALL FOUR dimensions, on BOTH budgets, before ANY of them
            #    is incremented. Checking-then-reserving per dimension
            #    would let money pass and be held while bytes denied.
            _check_dimensions(tenant_budget, wanted, scope=f"workspace:{workspace_id}")
            _check_dimensions(fleet_budget, wanted, scope=f"provider:{req.provider}")

            _reserve(tenant_budget, wanted)
            _reserve(fleet_budget, wanted)

            authorization_id = new_uuid7()
            decision_version = _decision_version(period, tenant_budget, fleet_budget)
            lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            reservation = CostReservation(
                id=new_uuid7(),
                workspace_id=workspace_id,
                authorization_id=authorization_id,
                state=ReservationState.RESERVED,
                purpose=req.purpose,
                domain=req.domain,
                transport=str(req.transport),
                provider=req.provider,
                scrape_job_id=req.scrape_job_id,
                dedupe_key=req.dedupe_key,
                budget_decision_version=decision_version,
                entitlement_version=entitlement_version,
                breaker_decision=breaker_decision,
                currency=req.currency,
                reserved_cost_micro_units=req.estimated_cost_micro_units,
                reserved_bytes=req.estimated_bytes,
                reserved_requests=req.estimated_requests,
                reserved_browser_seconds=req.estimated_browser_seconds,
                lease_expires_at=lease_expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)

            self._emit_threshold_warnings(
                session,
                workspace_id=workspace_id,
                budgets=((tenant_budget, f"workspace:{workspace_id}"),
                         (fleet_budget, f"provider:{req.provider}")),
                now=now,
                period=period,
            )

            session.commit()

        logger.info(
            "cost_authorization.granted workspace_id=%s domain=%s purpose=%s "
            "provider=%s transport=%s cost_micro_units=%d bytes=%d authorization_id=%s",
            workspace_id,
            req.domain,
            req.purpose.value,
            req.provider,
            req.transport,
            req.estimated_cost_micro_units,
            req.estimated_bytes,
            authorization_id,
        )
        return AuthorizationGrant(
            authorization_id=authorization_id,
            workspace_id=workspace_id,
            budget_decision_version=decision_version,
            lease_expires_at=lease_expires_at,
            reserved_cost_micro_units=req.estimated_cost_micro_units,
            reserved_bytes=req.estimated_bytes,
            reserved_requests=req.estimated_requests,
            reserved_browser_seconds=req.estimated_browser_seconds,
            currency=req.currency,
            entitlement_version=entitlement_version,
            breaker_decision=breaker_decision,
        )

    def heartbeat(self, authorization_id: uuid.UUID | str) -> None:
        """Renew the lease on a live reservation.

        A terminal (settled/released) reservation is a **no-op**, not an
        error: a heartbeat racing its own settlement is ordinary, and
        raising would turn a benign race into an alert. An
        ``authorization_id`` that names no reservation at all raises
        :class:`LookupError` — that is not a race, it is a caller bug or a
        lost row, and silently succeeding would hide it.
        """
        auth_id = _as_uuid(authorization_id)
        now = self._now()
        workspace_id = self._workspace_for(auth_id)
        with self._tenant_session(workspace_id) as session:
            result = session.execute(
                update(CostReservation)
                .where(
                    CostReservation.authorization_id == auth_id,
                    CostReservation.state == ReservationState.RESERVED,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount == 0:
                logger.debug(
                    "cost_authorization.heartbeat_noop authorization_id=%s "
                    "(reservation already terminal)",
                    auth_id,
                )

    def settle_partial(
        self, authorization_id: uuid.UUID | str, delta: SettledCost
    ) -> None:
        """Accrue ONE operation's observed cost against a live grant.

        **Non-terminal.** The reservation stays ``RESERVED`` and its lease
        keeps running, because a grant covers a whole batch and one
        operation closing says nothing about whether the batch is done —
        see the module docstring's "One grant, MANY operations".

        What it does, per dimension: add ``delta`` to the grant's accrued
        ``settled_*`` and to the budget's ``settled_*``, and draw the same
        amount DOWN from the budget's ``reserved_*`` — but never more than
        this grant is actually holding there. An operation that overruns
        the batch's estimate therefore lands as real spend rather than as
        a negative hold, which is the only reading under which "used" stays
        equal to ``reserved + settled``.

        Idempotence is the CALLER's, not this method's: accumulation and
        compare-and-set cannot be the same operation. C4's recorder accrues
        only when its own ``closed_at IS NULL`` update actually closed the
        ledger row, so a replayed or redelivered close accrues nothing
        twice. A ``delta`` arriving after the grant is already terminal is
        logged loudly and DROPPED, exactly as a replayed :meth:`settle`
        is — double-charging a budget is worse than a visible warning, and
        the sweeper's ledger-liveness check makes the case unreachable
        while any operation under the grant is still open.
        """
        auth_id = _as_uuid(authorization_id)
        now = self._now()
        workspace_id = self._workspace_for(auth_id)

        with self._tenant_session(workspace_id) as session:
            reservation = _lock_reservation(session, auth_id)
            if reservation is None:
                raise LookupError(f"no cost reservation for authorization {auth_id}")
            if reservation.state is not ReservationState.RESERVED:
                logger.warning(
                    "cost_authorization.late_settlement authorization_id=%s state=%s "
                    "cost_micro_units=%d — an operation closed under a grant that is "
                    "already terminal; the spend is in the ledger but not in the budget",
                    auth_id,
                    reservation.state.value,
                    delta.cost_micro_units,
                )
                session.commit()
                return

            period = period_key_for(reservation.created_at)
            fleet_budget = _lock_fleet_budget(
                session, reservation.provider, period, reservation.currency
            )
            tenant_budget = _lock_tenant_budget(
                session, reservation.workspace_id, period, reservation.currency
            )

            self._accrue(
                session,
                reservation,
                _by_dimension(delta),
                budgets=(tenant_budget, fleet_budget),
                now=now,
                period=period,
            )
            session.commit()

    def settle(self, authorization_id: uuid.UUID | str, actual: SettledCost) -> None:
        """Accrue a FINAL ``actual`` and CAS ``RESERVED -> SETTLED``.

        The terminal half of settlement, and the only one a call site that
        can observe its work's end should use. ``actual`` is one more
        accrual, exactly like :meth:`settle_partial`'s ``delta``; what
        makes this call terminal is what follows it — the grant's RESIDUAL
        hold (``reserved − accrued``, floored at zero, per dimension) goes
        back to both budgets and the row moves to ``SETTLED``.

        For the ordinary single-operation grant, where nothing accrued
        beforehand, that is precisely the old behaviour: settling 4 against
        a 10-unit hold leaves 4 spent and 6 returned. For a grant that
        already accrued 20 across twenty operations, ``settle(zero)``
        returns whatever is left of the hold and settles nothing more.

        Idempotent by construction: the reservation row is locked, and a
        row that is not ``RESERVED`` returns without touching a counter.
        That is why a replayed settle of 4 against a 10-unit reservation
        leaves the budget at 96 rather than 92 — the second call finds a
        ``SETTLED`` row and does nothing.
        """
        auth_id = _as_uuid(authorization_id)
        now = self._now()
        workspace_id = self._workspace_for(auth_id)

        with self._tenant_session(workspace_id) as session:
            reservation = _lock_reservation(session, auth_id)
            if reservation is None:
                raise LookupError(f"no cost reservation for authorization {auth_id}")
            if reservation.state is not ReservationState.RESERVED:
                session.commit()
                return

            period = period_key_for(reservation.created_at)
            fleet_budget = _lock_fleet_budget(
                session, reservation.provider, period, reservation.currency
            )
            tenant_budget = _lock_tenant_budget(
                session, reservation.workspace_id, period, reservation.currency
            )

            self._accrue(
                session,
                reservation,
                _by_dimension(actual),
                budgets=(tenant_budget, fleet_budget),
                now=now,
                period=period,
            )
            # ...then hand back whatever of the hold this grant never used.
            _return_residual_hold(reservation, budgets=(tenant_budget, fleet_budget))

            reservation.state = ReservationState.SETTLED
            reservation.settled_at = now
            reservation.updated_at = now
            session.commit()

    def _accrue(
        self,
        session: Session,
        reservation: CostReservation,
        delta: dict[str, int],
        *,
        budgets: Sequence[Any],
        now: datetime,
        period: str,
    ) -> None:
        """Move ``delta`` from held to spent, on the row and both budgets.

        The one place the accrual arithmetic lives, so :meth:`settle` and
        :meth:`settle_partial` cannot drift apart on it.
        """
        prior = _accrued(reservation)
        _accrue_on_budgets(reservation, delta, prior=prior, budgets=budgets)
        reservation.settled_cost_micro_units = (
            prior["cost_micro_units"] + delta["cost_micro_units"]
        )
        reservation.settled_bytes = prior["bytes"] + delta["bytes"]
        reservation.settled_requests = prior["requests"] + delta["requests"]
        reservation.settled_browser_seconds = (
            prior["browser_seconds"] + delta["browser_seconds"]
        )
        reservation.updated_at = now

        # Settlement can cross a threshold the reservation did not (a
        # batch that overran its estimate), so the same once-per-period
        # announcement runs here too.
        self._emit_threshold_warnings(
            session,
            workspace_id=reservation.workspace_id,
            budgets=tuple(
                (budget, scope)
                for budget, scope in zip(
                    budgets,
                    (
                        f"workspace:{reservation.workspace_id}",
                        f"provider:{reservation.provider}",
                    ),
                    strict=True,
                )
            ),
            now=now,
            period=period,
        )

    def release(
        self,
        authorization_id: uuid.UUID | str,
        *,
        reason: str = RELEASE_REASON_FAILED_BEFORE_DISPATCH,
    ) -> None:
        """CAS ``RESERVED -> RELEASED``, returning the RESIDUAL hold. Idempotent.

        For failure BEFORE dispatch — its default reason and its ordinary
        case — nothing was spent, the residual IS the whole hold, and this
        returns all of it. For a grant that already accrued spend (a batch
        cancelled after some of its operations completed), it returns only
        what is left: money that was spent stays spent, because crediting
        it back would make a cancellation cheaper than a completion.

        A row that is already terminal returns without touching a counter,
        so a retry loop that releases twice cannot credit the budget twice.
        """
        auth_id = _as_uuid(authorization_id)
        now = self._now()
        workspace_id = self._workspace_for(auth_id)
        with self._tenant_session(workspace_id) as session:
            reservation = _lock_reservation(session, auth_id)
            if reservation is None:
                raise LookupError(f"no cost reservation for authorization {auth_id}")
            _release_locked(session, reservation, now=now, reason=reason)
            session.commit()

    def assert_entitled(self, workspace_id: uuid.UUID | str | None = None) -> None:
        """Raise ``ENTITLEMENT_INACTIVE`` unless the workspace is entitled.

        The entitlement gate ALONE — no breaker, no domain state, no
        reservation. It exists for the fan-out routes
        (``POST /v1/variants/{id}/rescrape``, ``POST /v1/jobs/run/variant/{id}``)
        that create a job spanning SEVERAL competitor domains: those
        cannot honestly price or authorize a single grant, so their paid
        work is authorized per batch inside ``dispatch_job``, which knows
        each batch's domain and mode.

        What those routes still owe their caller is the ONE denial that
        does not depend on any domain and that the user can act on: an
        inactive or unpaid account. Without this they would answer "202
        accepted" and then dispatch nothing, which is the exact
        looks-accepted-never-happens outcome the C3 gate exists to
        remove. Everything else — budget, breaker, domain certification —
        is genuinely per-batch and correctly reported by the worker, not
        guessed at by the route.
        """
        ws = _as_uuid(workspace_id) if workspace_id is not None else self._default_workspace_id
        if ws is None:
            raise ValueError(
                "assert_entitled needs a workspace_id (or a default_workspace_id)"
            )
        with self._tenant_session(ws) as session:
            self._check_entitlement(session, ws, self._now())
            session.rollback()

    def remaining_budget_micro_units(
        self,
        workspace_id: uuid.UUID | str | None = None,
        *,
        period_key: str | None = None,
    ) -> int | None:
        """Money left on a tenant budget: ``limit - (reserved + settled)``.

        ``None`` when the budget has no money ceiling (a ``NULL`` limit —
        "no ceiling on that dimension", the posture inherited from
        ``access.budget``), and ``None`` when no budget row exists for the
        period yet, because an absent row is an absent ceiling rather than
        a zero one.

        A reservation counts against "remaining" the instant it is taken,
        not when it settles — that is the whole point of reserving.
        """
        ws = _as_uuid(workspace_id) if workspace_id is not None else self._default_workspace_id
        if ws is None:
            raise ValueError(
                "remaining_budget_micro_units needs a workspace_id (or a "
                "default_workspace_id on the service)"
            )
        period = period_key or period_key_for(self._now())
        with self._tenant_session(ws) as session:
            budget = session.execute(
                select(CostBudget).where(
                    CostBudget.workspace_id == ws, CostBudget.period_key == period
                )
            ).scalar_one_or_none()
            if budget is None or budget.limit_cost_micro_units is None:
                return None
            return int(budget.limit_cost_micro_units) - (
                int(budget.reserved_cost_micro_units) + int(budget.settled_cost_micro_units)
            )

    # -- gates --------------------------------------------------------------

    def _check_entitlement(
        self, session: Session, workspace_id: uuid.UUID, now: datetime
    ) -> str | None:
        """Deny unless durable local evidence says ACTIVE **and** is fresh.

        Three fail-closed cases, all reported as ``ENTITLEMENT_INACTIVE``
        because all three mean the same thing to a dispatch: no row, stale
        evidence, non-ACTIVE state. See the model module for why this
        evidence is local at all.

        Returns the evidence version to stamp on the grant. **Returned,
        not stashed on ``self``** — one service instance is shared by
        every thread in a worker process, so an instance attribute here
        would be a cross-request data race that only shows up under the
        concurrency the budget lock exists to handle.
        """
        row = session.execute(
            select(WorkspaceEntitlement).where(
                WorkspaceEntitlement.workspace_id == workspace_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise CostAuthorizationDenied(
                DenialReason.ENTITLEMENT_INACTIVE,
                f"no durable entitlement evidence for workspace {workspace_id}",
            )
        age = (now - _as_aware(row.observed_at)).total_seconds()
        if age > self._entitlement_max_evidence_age_seconds:
            raise CostAuthorizationDenied(
                DenialReason.ENTITLEMENT_INACTIVE,
                f"entitlement evidence for workspace {workspace_id} is "
                f"{int(age)}s old (max {self._entitlement_max_evidence_age_seconds}s); "
                "staleness is treated as inactive",
            )
        if row.state is not EntitlementState.ACTIVE:
            raise CostAuthorizationDenied(
                DenialReason.ENTITLEMENT_INACTIVE,
                f"workspace {workspace_id} entitlement state is {row.state.value}",
            )
        return row.evidence_version

    def _check_breaker(self, session: Session, now: datetime) -> str:
        """Deny on a missing/stale/OPEN breaker. Returns the recorded verdict.

        Fail-closed by the run's binding ruling: no row and stale evidence
        both DENY. The breaker's evaluator re-runs on its own lease far
        more often than the max evidence age, so a stale row means the
        evaluator has stopped — the exact condition under which "we have
        no idea what we are spending" is the honest answer.
        """
        row = session.execute(
            select(ProxyCircuitBreaker).where(
                ProxyCircuitBreaker.scope_key == self._breaker_scope_key
            )
        ).scalar_one_or_none()
        if row is None:
            raise CostAuthorizationDenied(
                DenialReason.BREAKER_EVIDENCE_STALE,
                f"no proxy_circuit_breakers row for scope {self._breaker_scope_key!r} — "
                "absent breaker evidence is not permission",
            )
        age = (now - _as_aware(row.evaluated_at)).total_seconds()
        if age > self._breaker_max_evidence_age_seconds:
            raise CostAuthorizationDenied(
                DenialReason.BREAKER_EVIDENCE_STALE,
                f"breaker evidence is {int(age)}s old "
                f"(max {self._breaker_max_evidence_age_seconds}s)",
            )
        if row.state is not ProxyBreakerState.CLOSED:
            raise CostAuthorizationDenied(
                DenialReason.BREAKER_OPEN,
                f"breaker is {row.state.value}"
                + (f" ({row.trip_reason.value})" if row.trip_reason else ""),
            )
        return row.state.value

    def _check_domain(self, session: Session, req: AuthorizationRequest) -> None:
        """Apply C2's rule table at the gate this request needs.

        See the module docstring's "Reconciling C2's nested rules" section
        for why three reasons rather than one.
        """
        state = get_domain_state(session, req.domain)
        rules = authorization_rules_for_state(state)
        gate = _required_domain_gate(req.purpose, req.transport)

        if not rules.paid_allowed:
            raise CostAuthorizationDenied(
                DenialReason.DOMAIN_QUARANTINED,
                f"domain {req.domain!r} is {state.value}: all paid work denied",
            )
        if gate in (_DomainGate.BROAD_CRAWL, _DomainGate.EXPENSIVE_ESCALATION) and (
            not rules.broad_crawl_allowed
        ):
            raise CostAuthorizationDenied(
                DenialReason.DOMAIN_NOT_CERTIFIED,
                f"domain {req.domain!r} is {state.value}: broad crawl denied "
                f"(purpose {req.purpose.value} on {req.transport} is not the "
                "tiny DIRECT canary UNKNOWN permits)",
            )
        if gate is _DomainGate.EXPENSIVE_ESCALATION and not rules.expensive_escalation_allowed:
            raise CostAuthorizationDenied(
                DenialReason.DOMAIN_ESCALATION_DENIED,
                f"domain {req.domain!r} is {state.value}: expensive escalation denied",
            )

    def _check_concurrency(
        self,
        session: Session,
        workspace_id: uuid.UUID,
        budget: CostBudget,
        now: datetime,
        *,
        req: "AuthorizationRequest | None" = None,
    ) -> None:
        """Deny when the workspace already holds its cap in LIVE grants.

        "Live" means ``RESERVED`` with an unexpired lease. A reservation
        whose lease has lapsed is not counted here even though it is still
        ``RESERVED`` — the sweeper may not have reached it yet, and making
        a stuck sweeper able to wedge a workspace's whole concurrency
        budget would turn a maintenance lag into an outage.

        **Tenant-scoped, and staying that way (EPA B5/F10).** The cap
        counted here is this workspace's own; fleet-wide host admission
        is emphatically NOT decided in this method. It is decided by
        ``app_shared.limiter.fleet.admit_fleet``'s Redis lease at the
        physical request boundary, because that is the only place where
        check and act are atomic across every worker in the fleet — a
        SQL count here would be a check-then-act race the moment two
        workspaces authorized concurrently, which is precisely the bug
        the lease exists to remove.

        What the fleet contributes here is a *reason*, never a verdict:
        when ``fleet_snapshot_reader`` is configured (it is ``None`` by
        default, and then this method's messages are byte-identical to
        before), a denial's detail also names how much fleet admission
        pressure the domain is under, so an operator reading "workspace X
        holds 4 live reservations (cap 4)" can tell at a glance whether
        the fleet was also saturated on that host. The snapshot is stale
        the instant it is read and nothing branches on it.
        """
        cap = budget.max_concurrent_reservations
        if cap is None:
            return
        live = session.execute(
            select(func.count())
            .select_from(CostReservation)
            .where(
                CostReservation.workspace_id == workspace_id,
                CostReservation.state == ReservationState.RESERVED,
                CostReservation.lease_expires_at > now,
            )
        ).scalar_one()
        if int(live) >= int(cap):
            detail = f"workspace {workspace_id} holds {live} live reservations (cap {cap})"
            fleet_detail = self._fleet_pressure_detail(req)
            if fleet_detail:
                detail = f"{detail}; {fleet_detail}"
            raise CostAuthorizationDenied(
                DenialReason.CONCURRENCY_CAP_EXCEEDED,
                detail,
            )

    def _fleet_pressure_detail(self, req: "AuthorizationRequest | None") -> str:
        """Render the fleet admission snapshot for a denial message, or
        ``""`` when no reader is configured (the default).

        Reasons only — see :meth:`_check_concurrency`. Never raises and
        never branches anything: a denial *message* must not be able to
        break the denial it is explaining, so any error resolves to no
        extra detail at all.
        """
        reader = self._fleet_snapshot_reader
        if reader is None or req is None:
            return ""
        try:
            snapshot = reader(req.domain, req.transport)
        except Exception:  # noqa: BLE001 - a reason may never break a decision
            logger.warning(
                "costauth: fleet snapshot unavailable for domain=%s", req.domain, exc_info=True
            )
            return ""
        if snapshot is None:
            return ""
        return (
            f"fleet admission on {snapshot.domain}/{snapshot.transport}: "
            f"{snapshot.in_flight}/{snapshot.concurrency} in flight"
        )

    # -- warnings -----------------------------------------------------------

    def _emit_threshold_warnings(
        self,
        session: Session,
        *,
        workspace_id: uuid.UUID,
        budgets: Sequence[tuple[Any, str]],
        now: datetime,
        period: str,
    ) -> None:
        """Announce newly-crossed 50/75/90% marks, at most once per period.

        The already-announced marks live on the budget row itself, so:

        * a replay cannot re-announce (the row already lists the mark);
        * a drained outbox cannot cause a re-announce either, which a
          "have we already enqueued this?" check against
          ``outbox_messages`` would (a drained row is gone).

        The outbox ``task_name`` is the EXISTING ``create_webhook_event``
        consumer — never a new, unregistered name (EPA B7's ruling). The
        message is written into the caller's transaction, so a warning
        cannot become durable unless the reservation that triggered it did.
        """
        for budget, scope in budgets:
            limit = budget.limit_cost_micro_units
            if not limit:
                continue
            used = int(budget.reserved_cost_micro_units) + int(
                budget.settled_cost_micro_units
            )
            pct = (used * 100) // int(limit)
            already = {int(v) for v in (budget.warned_thresholds or [])}
            crossed = [t for t in BUDGET_WARNING_THRESHOLDS if pct >= t and t not in already]
            if not crossed:
                continue
            for threshold in crossed:
                dedup_key = f"budget-warn:{threshold}:{scope}:{period}"
                message_id = new_uuid7()
                write_outbox_message(
                    session,
                    workspace_id=workspace_id,
                    task_name=CREATE_WEBHOOK_EVENT,
                    queue="webhook_events",
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "event_type": BUDGET_WARNING_EVENT_TYPE,
                        "payload": {
                            "scope": scope,
                            "period_key": period,
                            "threshold_pct": threshold,
                            "used_pct": pct,
                            "used_micro_units": used,
                            "limit_micro_units": int(limit),
                            "currency": budget.currency,
                        },
                        "dedup_key": dedup_key,
                        "event_id": str(message_id),
                        "occurred_at": now.isoformat(),
                    },
                    dedup_key=dedup_key,
                    now=now,
                    message_id=message_id,
                )
            # Reassign rather than mutate: a JSONB column mutated in place
            # is not seen as dirty by the unit of work, so the crossings
            # would be announced again on the next call.
            budget.warned_thresholds = sorted(already | set(crossed))
            budget.updated_at = now

    # -- helpers ------------------------------------------------------------

    def _workspace_for(self, authorization_id: uuid.UUID) -> uuid.UUID:
        """Which workspace owns ``authorization_id`` — id resolution ONLY.

        ``heartbeat``/``settle``/``release`` are addressed by grant id, and
        the workspace GUC must be set before the row can be read under
        forced RLS — a chicken-and-egg the rest of this repository already
        solves the same way (``cancellation._resolve_workspace_id``):
        resolve the id, and *only* the id, on the sanctioned BYPASSRLS
        system role, then do every row read and write inside the tenant
        scope. No row content crosses a tenant boundary.

        A ``default_workspace_id`` short-circuits it entirely, which is
        what a caller holding a grant it just minted should supply.
        """
        if self._default_workspace_id is not None:
            return self._default_workspace_id
        with self._system_session() as session:
            rows = session.execute(
                select(CostReservation.workspace_id).where(
                    CostReservation.authorization_id == authorization_id
                )
            ).all()
        if not rows:
            raise LookupError(f"no cost reservation for authorization {authorization_id}")
        return rows[0].workspace_id


# ---------------------------------------------------------------------------
# Module-level operations (the sweeper + cancellation's release)
# ---------------------------------------------------------------------------


def sweep_expired_reservations(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 500,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[int, int]:
    """Reap expired leases — but ONLY those with no open operation.

    ``session`` must be on the sanctioned BYPASSRLS **system** seam: the
    sweep is inherently cross-tenant, and it reads ``network_operations``,
    which has no ``workspace_id`` to scope by at all.

    For each ``RESERVED`` reservation whose lease has lapsed, the ledger is
    asked whether an operation opened under that ``authorization_id`` is
    still open (``closed_at IS NULL``):

    * **open** — the reservation is left ``RESERVED`` and its lease is
      extended. An expired lease means a heartbeat stopped, which happens
      whenever a worker is paused or slow; it is not evidence the work
      stopped, and releasing a live operation's money is how a budget ends
      up spent twice while its counters look healthy.
    * **not open** — released, reason ``LEASE_EXPIRED``, RESIDUAL hold
      returned to both budgets (whatever this grant's operations did not
      already accrue through ``settle_partial`` — spent money is never
      credited back).

    That second bullet is also the TERMINAL CLOSE for a batch grant: the
    worker POSTs a batch to a remote Scrapyd node and cannot observe when
    the last of its operations finishes, so nothing in-process is entitled
    to declare the grant done. This sweep is, and only because it asks the
    ledger first — while any operation is open the lease is extended, so
    "the lease lapsed AND the ledger holds nothing open" is the one
    statement that means the batch is over. See the module docstring's
    "One grant, MANY operations".

    Returns ``(released, skipped_live)``. Rows are locked ``FOR UPDATE
    SKIP LOCKED`` so two sweepers never contend and neither blocks a
    settle in flight.
    """
    moment = now or datetime.now(timezone.utc)
    expired = (
        session.execute(
            select(CostReservation)
            .where(
                CostReservation.state == ReservationState.RESERVED,
                CostReservation.lease_expires_at < moment,
            )
            .order_by(CostReservation.lease_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    released = 0
    skipped_live = 0
    for reservation in expired:
        if _has_open_operation(session, reservation.authorization_id):
            reservation.lease_expires_at = moment + timedelta(seconds=lease_seconds)
            reservation.updated_at = moment
            skipped_live += 1
            logger.info(
                "cost_authorization.lease_extended authorization_id=%s "
                "(operation still open in the ledger)",
                reservation.authorization_id,
            )
            continue
        _release_locked(
            session, reservation, now=moment, reason=RELEASE_REASON_LEASE_EXPIRED
        )
        released += 1

    session.commit()
    return released, skipped_live


def _has_open_operation(session: Session, authorization_id: uuid.UUID) -> bool:
    """True if C1's ledger holds an OPEN operation for ``authorization_id``.

    "Closed" is ``closed_at IS NOT NULL`` — the single column C1's
    immutability trigger itself reads to decide whether a row may still be
    written. Using the same predicate the database uses means this check
    cannot drift from the ledger's own definition of closed.
    """
    return (
        session.execute(
            select(NetworkOperation.id)
            .where(
                NetworkOperation.authorization_id == authorization_id,
                NetworkOperation.closed_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def release_reservations_for_scrape_job(
    session: Session,
    *,
    workspace_id: uuid.UUID | str | None,
    scrape_job_id: uuid.UUID | str,
    now: datetime | None = None,
    reason: str = RELEASE_REASON_CANCELLED_JOB,
) -> int:
    """Release every live reservation held by one scrape job. Idempotent.

    Called by ``app_shared.jobs.cancellation`` as step 4 of the
    cancellation protocol. ``session`` must already be inside an open,
    workspace-scoped transaction; this function does not commit (its
    caller owns the transaction boundary, exactly as the module's ordering
    protocol requires).

    Returns how many reservations this call moved to ``RELEASED``. A
    second call returns 0 — every row it would touch is already terminal —
    which is what makes re-running a crashed cancellation safe.
    """
    moment = now or datetime.now(timezone.utc)
    job_uuid = _as_uuid(scrape_job_id)
    stmt = select(CostReservation).where(
        CostReservation.scrape_job_id == job_uuid,
        CostReservation.state == ReservationState.RESERVED,
    )
    if workspace_id is not None:
        # Assert the tenant boundary in the SQL, never merely in the GUC
        # (Principle II / scripts/check_workspace_scoping.py) — the same
        # posture every other query on the cancellation path takes.
        stmt = stmt.where(CostReservation.workspace_id == _as_uuid(workspace_id))

    reservations = list(session.execute(stmt).scalars().all())
    for reservation in reservations:
        _release_locked(session, reservation, now=moment, reason=reason)
    return len(reservations)


# ---------------------------------------------------------------------------
# Row-level primitives (all assume an OPEN transaction)
# ---------------------------------------------------------------------------


def _lock_reservation(session: Session, authorization_id: uuid.UUID) -> CostReservation | None:
    """The reservation for ``authorization_id``, locked ``FOR UPDATE``.

    The lock is what makes ``settle``/``release`` compare-and-set rather
    than read-then-hope: two concurrent settles serialize here, and the
    loser finds a terminal row.
    """
    return session.execute(
        select(CostReservation)
        .where(CostReservation.authorization_id == authorization_id)
        .with_for_update()
    ).scalar_one_or_none()


def _get_or_create_budget_locked(
    session: Session, model: Any, *, keys: dict[str, Any], currency: str
) -> Any:
    """The budget row for ``keys``, locked ``FOR UPDATE``, created if absent.

    Created on first use with **NULL limits** — "no ceiling on that
    dimension", the posture
    ``access.budget.incr_and_check_monthly_budget`` already takes for an
    unconfigured provider. A row that materialises itself with an invented
    ceiling would deny work nobody ever budgeted for; a row that
    materialises with no ceiling still counts, so the operator who later
    sets a limit sets it against real numbers.

    **Why the creation is an ``ON CONFLICT DO NOTHING`` and not an
    ``INSERT``.** The whole point of this function is to be called
    concurrently — twenty authorizations racing for one budget row is the
    contract's own headline test. On the very first authorization of a
    period, all twenty find no row and all twenty try to create it; a
    plain INSERT means nineteen ``UniqueViolation``s, each of which
    poisons its whole transaction and turns a *successful* authorization
    into an error. ``ON CONFLICT DO NOTHING`` makes the loser's insert a
    silent no-op that waits for the winner to commit, after which the
    re-SELECT below finds the row and takes the lock normally. The race is
    therefore invisible rather than fatal, which is what "the budget row
    materialises on first use" has to mean if first use can be
    concurrent.
    """
    stmt = select(model).filter_by(**keys).with_for_update()
    budget = session.execute(stmt).scalar_one_or_none()
    if budget is not None:
        return budget

    moment = datetime.now(timezone.utc)
    session.execute(
        pg_insert(model.__table__)
        .values(id=new_uuid7(), currency=currency, created_at=moment, updated_at=moment, **keys)
        .on_conflict_do_nothing()
    )
    budget = session.execute(stmt).scalar_one_or_none()
    if budget is None:  # pragma: no cover - only reachable if the row is deleted mid-flight
        raise CostAuthorizationError(
            f"could not materialise a {model.__tablename__} row for {keys}"
        )
    return budget


def _lock_tenant_budget(
    session: Session, workspace_id: uuid.UUID, period: str, currency: str
) -> CostBudget:
    """The workspace's budget row for ``period``, locked ``FOR UPDATE``."""
    return _get_or_create_budget_locked(
        session,
        CostBudget,
        keys={"workspace_id": workspace_id, "period_key": period},
        currency=currency,
    )


def _lock_fleet_budget(
    session: Session, provider: str, period: str, currency: str
) -> FleetCostBudget:
    """The provider's fleet budget row for ``period``, locked ``FOR UPDATE``.

    Always locked BEFORE the tenant row (see ``authorize``): one fixed
    global order over the two budget rows every decision touches is what
    makes concurrent authorizations against a shared provider serialize
    instead of deadlock.
    """
    return _get_or_create_budget_locked(
        session,
        FleetCostBudget,
        keys={"scope_key": provider, "period_key": period},
        currency=currency,
    )


def _check_dimensions(budget: Any, wanted: dict[str, int], *, scope: str) -> None:
    """Raise on the FIRST dimension ``wanted`` would push past its limit.

    Walks :data:`_DIMENSIONS` — money, bytes, requests, browser-seconds —
    and denies with that dimension's own reason. Nothing is mutated here,
    which is what lets the caller check BOTH budgets across ALL four
    dimensions before reserving any of them.
    """
    for dimension in _DIMENSIONS:
        limit = getattr(budget, dimension.limit_column)
        if limit is None:
            continue
        used = int(getattr(budget, dimension.reserved_column)) + int(
            getattr(budget, dimension.settled_column)
        )
        want = int(wanted[dimension.name])
        if used + want > int(limit):
            raise CostAuthorizationDenied(
                dimension.reason,
                f"{scope} {dimension.name}: {used} used + {want} requested "
                f"exceeds limit {limit} (period budget)",
            )


def _reserve(budget: Any, wanted: dict[str, int]) -> None:
    """Increment every ``reserved_*`` counter and bump ``decision_version``."""
    for dimension in _DIMENSIONS:
        setattr(
            budget,
            dimension.reserved_column,
            int(getattr(budget, dimension.reserved_column)) + int(wanted[dimension.name]),
        )
    budget.decision_version = int(budget.decision_version) + 1


def _by_dimension(cost: SettledCost) -> dict[str, int]:
    """A :class:`SettledCost` in the four dimensions' own vocabulary."""
    return {
        "cost_micro_units": int(cost.cost_micro_units),
        "bytes": int(cost.bytes_used),
        "requests": int(cost.requests),
        "browser_seconds": int(cost.browser_seconds),
    }


def _holds(reservation: CostReservation) -> dict[str, int]:
    """What this grant reserved, per dimension."""
    return {
        "cost_micro_units": int(reservation.reserved_cost_micro_units),
        "bytes": int(reservation.reserved_bytes),
        "requests": int(reservation.reserved_requests),
        "browser_seconds": int(reservation.reserved_browser_seconds),
    }


def _accrued(reservation: CostReservation) -> dict[str, int]:
    """What this grant has ALREADY settled, per dimension.

    ``NULL`` means "nothing yet" rather than zero-the-quantity, which is
    why every read goes through here instead of touching the columns.
    """
    return {
        "cost_micro_units": int(reservation.settled_cost_micro_units or 0),
        "bytes": int(reservation.settled_bytes or 0),
        "requests": int(reservation.settled_requests or 0),
        "browser_seconds": int(reservation.settled_browser_seconds or 0),
    }


def _accrue_on_budgets(
    reservation: CostReservation,
    delta: dict[str, int],
    *,
    prior: dict[str, int],
    budgets: Sequence[Any],
) -> None:
    """``settled_* += delta``; ``reserved_* -=`` the part of it still held.

    The "still held" clamp is the whole arithmetic. A grant holding 20
    that accrues 1 twenty times draws its hold down by exactly 20; the
    twenty-first accrual (an overrun) adds to ``settled_*`` and takes
    nothing further from ``reserved_*``, because there is nothing left to
    take. Without the clamp an overrunning batch would drive the budget's
    ``reserved_*`` negative — a silent, permanent over-grant.
    """
    held = _holds(reservation)
    for dimension in _DIMENSIONS:
        name = dimension.name
        drawn = max(0, min(prior[name] + delta[name], held[name]) - min(prior[name], held[name]))
        for budget in budgets:
            setattr(
                budget,
                dimension.reserved_column,
                max(0, int(getattr(budget, dimension.reserved_column)) - drawn),
            )
            setattr(
                budget,
                dimension.settled_column,
                int(getattr(budget, dimension.settled_column)) + int(delta[name]),
            )
    for budget in budgets:
        budget.decision_version = int(budget.decision_version) + 1


def _return_residual_hold(
    reservation: CostReservation, *, budgets: Sequence[Any]
) -> None:
    """Hand back the part of the hold this grant never spent. Terminal only.

    ``residual = reserved − accrued``, floored at zero per dimension —
    the exact complement of what :func:`_accrue_on_budgets` already drew
    down, so the two together return each reserved unit exactly once.
    Clamped at zero on the budget purely defensively: a negative counter
    would be a silent, permanent over-grant, and clamping makes a bug show
    up as a stuck counter (visible) instead of free money (invisible).
    """
    held = _holds(reservation)
    accrued = _accrued(reservation)
    for dimension in _DIMENSIONS:
        residual = max(0, held[dimension.name] - accrued[dimension.name])
        for budget in budgets:
            setattr(
                budget,
                dimension.reserved_column,
                max(0, int(getattr(budget, dimension.reserved_column)) - residual),
            )
    for budget in budgets:
        budget.decision_version = int(budget.decision_version) + 1


def _release_locked(
    session: Session, reservation: CostReservation, *, now: datetime, reason: str
) -> None:
    """CAS ``RESERVED -> RELEASED`` on an already-locked/loaded reservation.

    A no-op on a terminal row — that single guard is what makes
    ``release``, the sweeper and cancellation all individually idempotent
    and safe to interleave.

    Returns the RESIDUAL hold, never the whole reservation: a grant that
    already accrued spend through :meth:`~CostAuthorizationService.
    settle_partial` (a batch whose first operations completed before the
    job was cancelled, or whose lease lapsed after them) must not have
    that money credited back — it was spent. A released row therefore
    keeps its accrued ``settled_*`` and simply stops holding the rest;
    ``release_reason`` says which of the three paths got there.
    """
    if reservation.state is not ReservationState.RESERVED:
        return

    period = period_key_for(reservation.created_at)
    fleet_budget = _lock_fleet_budget(
        session, reservation.provider, period, reservation.currency
    )
    tenant_budget = _lock_tenant_budget(
        session, reservation.workspace_id, period, reservation.currency
    )
    _return_residual_hold(reservation, budgets=(tenant_budget, fleet_budget))

    reservation.state = ReservationState.RELEASED
    reservation.released_at = now
    reservation.release_reason = reason
    reservation.updated_at = now
    logger.info(
        "cost_authorization.released authorization_id=%s reason=%s",
        reservation.authorization_id,
        reason,
    )


def _decision_version(
    period: str, tenant_budget: CostBudget, fleet_budget: FleetCostBudget
) -> str:
    """The opaque tag naming the exact counter generation a grant used.

    Text, per C1's version-tag precedent (``entitlement_version`` /
    ``budget_decision_version`` on ``network_operations`` are both
    ``Text``): a later change of versioning scheme must not silently
    reinterpret an old decision, which is exactly what a bare integer
    would invite.
    """
    return (
        f"cb1:{period}:ws{int(tenant_budget.decision_version)}"
        f":fleet{int(fleet_budget.decision_version)}"
    )


def _grant_from(reservation: CostReservation, *, replayed: bool) -> AuthorizationGrant:
    return AuthorizationGrant(
        authorization_id=reservation.authorization_id,
        workspace_id=reservation.workspace_id,
        budget_decision_version=reservation.budget_decision_version,
        lease_expires_at=reservation.lease_expires_at,
        reserved_cost_micro_units=int(reservation.reserved_cost_micro_units),
        reserved_bytes=int(reservation.reserved_bytes),
        reserved_requests=int(reservation.reserved_requests),
        reserved_browser_seconds=int(reservation.reserved_browser_seconds),
        currency=reservation.currency,
        replayed=replayed,
        # A replayed grant must carry the SAME decision facts the original
        # did — they are stamped on operations, and an operation opened
        # under a redelivery must not claim a different entitlement
        # version than its twin.
        entitlement_version=reservation.entitlement_version,
        breaker_decision=reservation.breaker_decision,
    )


# ---------------------------------------------------------------------------
# Call-site helpers (the six paid dispatch sites use these)
# ---------------------------------------------------------------------------


def estimate_bytes(requests: int, *, per_request: int = DEFAULT_ESTIMATED_BYTES_PER_REQUEST) -> int:
    """Estimated transport bytes for ``requests`` fetches. Corrected at settle."""
    return max(1, int(requests)) * int(per_request)


def authorize_or_none(
    service: CostAuthorizationService, req: AuthorizationRequest, *, site: str
) -> AuthorizationGrant | None:
    """``service.authorize(req)``, returning ``None`` on a denial.

    The shape every **background** dispatch site wants: a denial is a
    reason to skip this unit of work and move on to the next, not a reason
    to fail the sweep. It is deliberately NOT what the API's synchronous
    manual-recheck route uses — there a denial has a user waiting for it
    and must become an HTTP status, not a silently skipped batch.

    Every denial is logged as a single structured line
    (``cost_authorization.denied``) carrying the site, so an operator can
    tell "the fleet stopped scraping" from "the fleet is scraping and
    being refused", which are completely different incidents.
    """
    try:
        return service.authorize(req)
    except CostAuthorizationDenied as denial:
        logger.warning(
            "cost_authorization.denied site=%s reason=%s workspace_id=%s domain=%s "
            "purpose=%s detail=%s",
            site,
            denial.reason.value,
            req.workspace_id,
            req.domain,
            req.purpose.value,
            denial.detail,
        )
        return None


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _as_aware(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Every timestamp column in this schema is ``TIMESTAMPTZ`` and
    ``TZDateTime`` refuses to store a naive value, so this only ever fires
    for a row written by something outside the ORM (a seed script, a
    manual operator UPDATE). Assuming UTC there is strictly better than
    raising, because the alternative is an evidence-freshness check that
    crashes instead of denying.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
