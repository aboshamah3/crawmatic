"""The cost-authorization store (EPA C3, READY-006).

C1 gave the fleet a place to record what a physical fetch **cost**. This
module gives it the row that decides, *before* the socket opens, whether
that fetch is allowed to happen at all — and reserves its estimated cost
so a second concurrent decision cannot spend the same money twice.

Four tables, each with a different owner and a different failure posture:

``cost_reservations``
    One row per authorization grant. Workspace-owned, RLS-forced. The
    row IS the grant: ``authorization_id`` is minted before anything is
    dispatched (the same pre-dispatch-identity discipline as C1's
    ``network_request_id``), and its ``state`` moves exactly once,
    ``RESERVED -> SETTLED`` or ``RESERVED -> RELEASED``, by
    compare-and-set. Every transition is therefore idempotent under
    replay: a second settle of a settled row changes nothing.

    ``lease_expires_at`` is a **lease, not a TTL**. A live operation
    renews it by heartbeat; an expired lease is only reaped after the
    sweeper has confirmed the ledger holds no OPEN
    ``network_operations`` row for that ``authorization_id``. An expired
    lease is evidence that a heartbeat stopped, never evidence that the
    work stopped — those are different facts, and releasing a live
    operation's money is how a budget ends up over-spent while its
    counters look healthy.

``cost_budgets``
    The tenant budget counters, workspace-owned and RLS-forced. One row
    per ``(workspace_id, period_key)``. **This is the authoritative
    store** — not Redis. Every check-and-reserve takes ``SELECT ... FOR
    UPDATE`` on this row, so N concurrent authorizations serialize on it
    and the hard ceiling is a database property rather than a race the
    application hopes it wins. Redis may cache *denials* for fast-path
    rejection; it never grants (§ the C3 contract).

``fleet_cost_budgets``
    The same counters for a FLEET-wide provider budget, keyed by
    ``scope_key`` (the provider). Deliberately carries **no**
    ``workspace_id`` and no RLS — exactly the shape (and for exactly the
    reason) of ``proxy_circuit_breakers``: the money is spent against one
    shared provider account, so one tenant's runaway loop spends
    everyone's budget and every workspace's scrape path must be able to
    read and decrement the same counter. A tenant-scoped policy here
    would make the fleet ceiling invisible to the connections it exists
    to stop.

``workspace_entitlements``
    The **durable local entitlement evidence** the denial path reads.
    Workspace-owned, RLS-forced. See "Entitlement evidence" below.

All four counter dimensions in one place
----------------------------------------
Money (integer micro-USD — the ``app_shared.money`` §19 "money is never
a float" contract, expressed the same way C1's ledger expresses it),
bytes, request count, and browser-seconds are separate columns on the
same budget row, checked and decremented inside the same transaction and
under the same row lock. That is what makes "one dimension cannot pass
while another is exceeded" a structural property instead of an ordering
convention: there is no window in which money has been reserved and bytes
have not.

A ``NULL`` limit means **no ceiling on that dimension**, and the counter
is still maintained. This mirrors
``app_shared.access.budget.incr_and_check_monthly_budget``'s existing
posture ("``limit is None`` -> always allowed") rather than inventing a
second, contradictory reading of an absent limit. Bounding an unlimited
provider is the independent circuit breaker's job, and the breaker is
consulted on every authorization.

Entitlement evidence: why this table exists here
------------------------------------------------
The C3 contract requires the entitlement denial to work "even when
SaaS/engine sync is down". A denial that has to ask the SaaS whether a
workspace is still paying is not a denial — it is an availability
dependency on the exact system whose outage the requirement is about. So
the evidence must be *local and durable*, and the engine had none: a
survey of this repository at EPA C3 found no engine-side entitlement
store at all (``grep -ril entitlement`` matched only C1's opaque
``network_operations.entitlement_version`` tag, which records which
evidence a dispatch was made under and cannot answer what the evidence
IS). ``workspaces.status`` is the closest existing column and is the
wrong fact: it is ``active``/``suspended`` operational lifecycle with no
notion of a billing plan and, critically, **no observation timestamp**,
so it cannot express staleness.

``workspace_entitlements`` is therefore the minimal durable evidence the
denial path needs, and nothing more:

* ``state`` — ``ACTIVE`` is the only value that authorizes paid work.
  ``PAST_DUE``/``SUSPENDED``/``CANCELLED`` all deny.
* ``observed_at`` — when the SaaS billing event that produced this row
  was replicated to the engine. **Staleness is treated as inactive**
  (W1.1's stated contract): evidence older than the configured maximum
  age denies exactly as a ``CANCELLED`` row does, and a workspace with no
  row at all denies too. Fail-closed in all three cases — no row, stale
  row, inactive row.
* ``evidence_version`` — the opaque SaaS-side version tag, recorded so a
  denial can be traced back to the billing event that caused it (and
  stamped onto ``network_operations.entitlement_version`` by C4). Text,
  for the same reason C1 made its version tags text: a later change of
  versioning scheme must not silently reinterpret an old decision.

W1.1 owns the SaaS half (``EngineDesiredState``/``ReconciliationRun`` and
the billing-event replication); this table is the engine half it writes
INTO, and is deliberately shaped as evidence-with-a-timestamp rather than
as a mirror of any SaaS model, so a schema change on the SaaS side cannot
turn into an engine migration.

RLS and who writes what
-----------------------
``cost_reservations``, ``cost_budgets`` and ``workspace_entitlements``
all carry ``workspace_id`` and all get the standard
:func:`app_shared.models.rls.emit_rls_policy` (ENABLE + FORCE + the
``workspace_id = <ctx>`` policy) in the creating migration —
``WORKSPACE`` class in ``scripts/rls_table_manifest.txt``.
``fleet_cost_budgets`` has no ``workspace_id`` and is declared
``SYSTEM``, like ``proxy_circuit_breakers``.

Two seams write here, and which one is used is a property of the path,
not a preference:

* **the tenant seam** (``crawmatic_app`` under
  ``SET LOCAL app.workspace_id``) — every ``authorize`` / ``heartbeat`` /
  ``settle`` / ``release`` call made by a worker or the API on behalf of
  one workspace. All of those touch exactly one workspace's rows, so the
  policy is a real guard rather than an obstacle.
* **the sanctioned BYPASSRLS system seam** (``crawmatic_auth`` /
  ``get_system_session``) — the lease sweeper ONLY. Reaping expired
  leases is inherently cross-tenant (one sweep must see every
  workspace's expired reservations) and it must additionally read
  ``network_operations``, a fleet-owned table with no workspace column
  at all. This is the same seam, for the same reason, as C1's
  allocation-total trigger and the scheduler's due-rule claim.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase

__all__ = [
    "BUDGET_WARNING_THRESHOLDS",
    "COST_RESERVATION_DEDUPE_INDEX",
    "COST_RESERVATION_DEDUPE_PREDICATE",
    "AuthorizationPurpose",
    "CostBudget",
    "CostReservation",
    "EntitlementState",
    "FleetCostBudget",
    "ReservationState",
    "WorkspaceEntitlement",
]

#: The percent-of-limit marks that emit a budget-warning event, in
#: ascending order. Each fires at most once per budget row per period —
#: the crossings that have already been announced are recorded durably on
#: the row itself (``warned_thresholds``) rather than inferred from the
#: outbox, so a warning cannot be re-emitted by a replay and cannot be
#: lost because the outbox row was already drained.
BUDGET_WARNING_THRESHOLDS: tuple[int, ...] = (50, 75, 90)


class ReservationState(StrEnum):
    """Lifecycle of one :class:`CostReservation`. Exactly one transition.

    ``RESERVED`` — money/bytes/requests/browser-seconds are held against
                   the budget counters. The only non-terminal state.
    ``SETTLED``  — the operation finished; the reservation was replaced by
                   its actual cost. Terminal.
    ``RELEASED`` — the operation never happened (failure before dispatch,
                   cancellation, or a swept expired lease with no open
                   operation in the ledger). The full reservation went
                   back to the budget. Terminal.

    Both transitions are compare-and-set from ``RESERVED``, which is what
    makes ``settle``/``release`` idempotent: a replay finds a terminal row
    and changes nothing rather than double-crediting the budget.
    """

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class AuthorizationPurpose(StrEnum):
    """Why the paid work is being requested — the C3 contract's six sites.

    Purpose is not decoration: it selects which of C2's three nested
    domain gates the request must clear (see
    ``app_shared.costauth.service._required_domain_gate``), and it is the
    only thing that distinguishes an ordinary refresh from a browser
    escalation once both have been reduced to a domain and a cost.
    """

    REFRESH = "REFRESH"
    FALLBACK = "FALLBACK"
    DISCOVERY = "DISCOVERY"
    MANUAL_RECHECK = "MANUAL_RECHECK"
    BROWSER_ESCALATION = "BROWSER_ESCALATION"
    RETRY = "RETRY"


class EntitlementState(StrEnum):
    """Durable local evidence of a workspace's billing entitlement.

    ``ACTIVE`` is the ONLY value that authorizes paid work. The other
    three are all denials with different causes, kept distinct so an
    operator reading a denial can tell a lapsed card from a deliberate
    cancellation without consulting the SaaS.
    """

    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


#: The dedupe index's predicate, kept next to the model for the same
#: reason ``OUTBOX_PENDING_PREDICATE`` is: an ``ON CONFLICT`` arbiter must
#: repeat the index predicate EXACTLY or Postgres cannot infer it, so the
#: two spellings must have exactly one source.
COST_RESERVATION_DEDUPE_PREDICATE = "state = 'RESERVED'"

#: Name of that partial unique index (created by the C3 migration, and
#: declared on the model below so ``alembic revision --autogenerate``
#: does not see it as drift and propose dropping it).
COST_RESERVATION_DEDUPE_INDEX = "uq_cost_reservations_live_dedupe_key"


class CostReservation(Base, WorkspaceScopedBase, TimestampMixin):
    """``cost_reservations`` — one authorization grant, one row.

    Workspace-owned and RLS-forced. ``authorization_id`` is the grant
    identity handed back to the caller and stamped onto
    ``network_operations.authorization_id`` by C4; it is unique, and it is
    the key ``heartbeat``/``settle``/``release`` address the row by. The
    surrogate ``id`` from :class:`Base` stays the row key.

    ``dedupe_key`` collapses a re-delivered request onto the grant it
    already has: a partial unique index over ``(workspace_id,
    dedupe_key)`` restricted to ``state = 'RESERVED'``
    (:data:`COST_RESERVATION_DEDUPE_INDEX`) means at most one LIVE grant
    can carry a given key while allowing the same key to be used again
    once the previous grant settled. The partial shape is deliberate and
    mirrors ``OUTBOX_PENDING_DEDUP_INDEX``: a full unique index would
    make a domain's second-ever refresh collide with its first.

    ``scrape_job_id`` is a plain nullable id with no foreign key —
    ``scrape_jobs`` is workspace-owned and monthly-partitioned downstream,
    and a reservation can legitimately belong to a fleet probe with no
    job (the same reasoning C1 applied to
    ``network_operations.scrape_job_id``). It exists so cancellation can
    find and release a cancelled job's outstanding grants.
    """

    __tablename__ = "cost_reservations"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_cost_reservations_authorization_id"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_cost_reservations_workspace_id_workspaces",
        ),
        CheckConstraint(
            "reserved_cost_micro_units >= 0 AND reserved_bytes >= 0 "
            "AND reserved_requests >= 0 AND reserved_browser_seconds >= 0",
            name="cr_reserved_non_negative",
        ),
        CheckConstraint(
            "settled_cost_micro_units IS NULL OR settled_cost_micro_units >= 0",
            name="cr_settled_cost_non_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="cr_currency_is_iso4217"),
        Index("ix_cost_reservations_state_lease_expires_at", "state", "lease_expires_at"),
        Index("ix_cost_reservations_scrape_job_id", "scrape_job_id"),
        # DECLARED, not merely migrated. The partial unique index is what
        # makes "at most one LIVE grant per dedupe key" true, and a
        # constraint the metadata does not know about is one
        # `alembic revision --autogenerate` will cheerfully propose
        # DROPping. Spelled to match the migration exactly — same name,
        # same columns, same predicate (both from the constants above, so
        # there is one source for each).
        Index(
            COST_RESERVATION_DEDUPE_INDEX,
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text(COST_RESERVATION_DEDUPE_PREDICATE),
        ),
    )

    #: The grant identity. Minted by :meth:`~app_shared.costauth.service.
    #: CostAuthorizationService.authorize` before anything is dispatched,
    #: so the grant exists even if the process dies mid-flight — the same
    #: pre-dispatch-identity discipline as C1's ``network_request_id``.
    authorization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    state: Mapped[ReservationState] = enum_column(
        ReservationState, nullable=False, default=ReservationState.RESERVED
    )
    purpose: Mapped[AuthorizationPurpose] = enum_column(
        AuthorizationPurpose, nullable=False
    )
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``app_shared.models.network_operations.NetworkTransport`` value —
    #: stored as free text rather than importing C1's enum into this
    #: table's DDL, so the two ledgers stay independently migratable.
    transport: Mapped[str] = mapped_column(String(length=32), nullable=False)
    provider: Mapped[str] = mapped_column(Text(), nullable=False)
    scrape_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Opaque tag naming the exact budget-counter generation this grant
    #: was decided against (text, per C1's version-tag precedent).
    budget_decision_version: Mapped[str] = mapped_column(Text(), nullable=False)
    #: The entitlement evidence version the grant was decided against, or
    #: NULL when the workspace's evidence row carried none.
    entitlement_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: What the breaker said at authorization time (CLOSED / OPEN / ...).
    breaker_decision: Mapped[str | None] = mapped_column(Text(), nullable=True)
    currency: Mapped[str] = mapped_column(String(length=3), nullable=False)

    # --- the four reserved dimensions, held while RESERVED --------------
    reserved_cost_micro_units: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    reserved_requests: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    reserved_browser_seconds: Mapped[int] = mapped_column(BigInteger(), nullable=False)

    # --- the four settled dimensions, written once at SETTLED -----------
    settled_cost_micro_units: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    settled_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    settled_requests: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    settled_browser_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )

    #: The lease. Renewed by ``heartbeat`` while the operation is live;
    #: past this instant the sweeper MAY reap the row — but only after
    #: confirming the ledger holds no open operation for it.
    lease_expires_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: Why the row was released (``CANCELLED_JOB``, ``LEASE_EXPIRED``,
    #: ``FAILED_BEFORE_DISPATCH``, ...). Free text: a release reason is a
    #: log line, not a decision anything branches on.
    release_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)


class _BudgetCountersMixin:
    """The four-dimension counter block shared by the two budget tables.

    Declared once because the tenant budget and the fleet provider budget
    are the SAME accounting object with different owners — letting the two
    column sets drift would mean one dimension could be enforced against a
    workspace and silently unenforced against the provider.

    ``NULL`` limit == no ceiling on that dimension (the counter is still
    maintained). ``reserved_*`` is money currently held by live grants;
    ``settled_*`` is money actually spent. "Used" is their sum, and
    "remaining" is ``limit - used`` — so a reservation constrains the next
    decision the instant it is taken, not when it settles.
    """

    limit_cost_micro_units: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    limit_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    limit_requests: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    limit_browser_seconds: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)

    reserved_cost_micro_units: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    reserved_bytes: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    reserved_requests: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    reserved_browser_seconds: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )

    settled_cost_micro_units: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    settled_bytes: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    settled_requests: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    settled_browser_seconds: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )

    #: Monotonic counter bumped by every mutation of this row. It is the
    #: budget half of the grant's ``budget_decision_version`` — the exact
    #: counter generation a decision was made against, so a later audit
    #: can say which reservations were decided before a limit change.
    decision_version: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
    #: Percent marks already announced for this row/period, e.g.
    #: ``[50, 75]``. JSONB rather than an int array so the shape matches
    #: every other JSON column in this schema and needs no array-type DDL.
    warned_thresholds: Mapped[list] = mapped_column(
        JSONB(), nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Cap on simultaneously-RESERVED grants for this scope. ``NULL`` ==
    #: uncapped.
    max_concurrent_reservations: Mapped[int | None] = mapped_column(
        Integer(), nullable=True
    )


class CostBudget(Base, WorkspaceScopedBase, TimestampMixin, _BudgetCountersMixin):
    """``cost_budgets`` — the TENANT budget counters. Workspace-owned, RLS'd.

    One row per ``(workspace_id, period_key)``, where ``period_key`` is
    the ``%Y_%m`` month key
    ``app_shared.access.budget._monthly_budget_key`` already uses — the
    same period boundary, so the durable counter and the legacy Redis
    counter can be compared directly during the cutover instead of
    disagreeing by a day.

    Every authorization takes ``SELECT ... FOR UPDATE`` on this row. That
    lock is the whole hard-ceiling mechanism: twenty concurrent
    authorizations against a one-dollar budget serialize here, and
    exactly ten of them find room.
    """

    __tablename__ = "cost_budgets"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "period_key", name="uq_cost_budgets_workspace_id_period_key"
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_cost_budgets_workspace_id_workspaces",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="cb_currency_is_iso4217"),
        CheckConstraint(
            "reserved_cost_micro_units >= 0 AND settled_cost_micro_units >= 0",
            name="cb_cost_counters_non_negative",
        ),
    )

    period_key: Mapped[str] = mapped_column(String(length=16), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(length=3), nullable=False, default="USD", server_default=text("'USD'")
    )


class FleetCostBudget(Base, TimestampMixin, _BudgetCountersMixin):
    """``fleet_cost_budgets`` — the FLEET provider budget. Global, no RLS.

    Keyed by ``(scope_key, period_key)``; ``scope_key`` is the provider
    identifier (``dataimpulse``, ...), mirroring
    ``proxy_circuit_breakers.scope_key``'s "granularity without a
    migration" trick.

    No ``workspace_id`` and no policy, deliberately and for the same
    reason the breaker has none: the balance is one shared operator
    account, so the ceiling must be visible to — and decrementable by —
    every workspace's scrape path. Declared ``SYSTEM`` in
    ``scripts/rls_table_manifest.txt``: it names a provider and a month,
    and holds no tenant-identifying column.
    """

    __tablename__ = "fleet_cost_budgets"
    __table_args__ = (
        UniqueConstraint(
            "scope_key", "period_key", name="uq_fleet_cost_budgets_scope_key_period_key"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="fcb_currency_is_iso4217"),
        CheckConstraint(
            "reserved_cost_micro_units >= 0 AND settled_cost_micro_units >= 0",
            name="fcb_cost_counters_non_negative",
        ),
    )

    scope_key: Mapped[str] = mapped_column(Text(), nullable=False)
    period_key: Mapped[str] = mapped_column(String(length=16), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(length=3), nullable=False, default="USD", server_default=text("'USD'")
    )


class WorkspaceEntitlement(Base, WorkspaceScopedBase, TimestampMixin):
    """``workspace_entitlements`` — the engine's durable entitlement evidence.

    One row per workspace (``workspace_id`` is unique). Written by the
    SaaS->engine billing replication (W1.1); read by
    :meth:`~app_shared.costauth.service.CostAuthorizationService.authorize`
    and by nothing else.

    The denial is fail-closed in three independent ways, which is the
    whole point of the table (see the module docstring):

    1. **no row** for a workspace -> denied;
    2. ``observed_at`` older than the configured maximum evidence age ->
       denied ("staleness treated as inactive", W1.1's stated contract);
    3. ``state`` anything but ``ACTIVE`` -> denied.

    None of the three needs the SaaS to be reachable, which is the
    requirement that made a local table necessary in the first place.
    """

    __tablename__ = "workspace_entitlements"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", name="uq_workspace_entitlements_workspace_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_entitlements_workspace_id_workspaces",
        ),
    )

    state: Mapped[EntitlementState] = enum_column(EntitlementState, nullable=False)
    #: The SaaS plan this evidence describes. Free text — the engine never
    #: branches on it, it only records what the denial was about.
    plan_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Opaque SaaS-side version tag (C1's version-tag precedent: text, so
    #: a scheme change cannot silently reinterpret an old decision).
    evidence_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: When the billing event behind this row was replicated to the
    #: engine. The freshness clock — NOT ``updated_at``, which any local
    #: touch would move and which would therefore make stale evidence
    #: look fresh.
    observed_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    #: EPA C1 (2026-09-03): the plan's product cap, as replicated from the
    #: SaaS alongside the rest of this evidence row. Read by the control
    #: plane's admission check ("may this workspace add another monitored
    #: product?").
    #:
    #: NULLABLE, and that is the whole design: ``NULL`` means *no ceiling
    #: was recorded* — an older evidence row written before this column
    #: existed, or a plan that does not express one — and must therefore
    #: NOT be read as a limit. ``0`` is a real, distinct value meaning
    #: "this plan allows zero products". A ``NOT NULL DEFAULT 0`` would
    #: have collapsed those two into one, silently capping every
    #: pre-existing workspace at zero the moment the migration ran.
    product_ceiling: Mapped[int | None] = mapped_column(Integer(), nullable=True)
