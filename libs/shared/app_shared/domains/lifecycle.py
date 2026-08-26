"""``transition`` — the full domain certification lifecycle (EPA W4.1,
report §6; builds on C2's minimum).

C2 (``app_shared.domains.state_lookup``, migration ``d9a46612bcc8``)
shipped the authorization-facing minimum: a ``DomainState`` enum, a
``domain_playbooks.state`` column, and the deny-by-state rule table C3
consults. It deliberately stopped there — "the approval workflow,
transition audit trail, and admin UI for moving a domain between these
states all remain W4.1 scope" (its own module docstring). This module
is that remaining scope:

* :func:`transition` — the single writer of ``domain_playbooks.state``.
  Enforces the lifecycle graph (:data:`ALLOWED_TRANSITIONS`), requires
  a recorded human ``approver`` for every **capability-granting** edge
  (:func:`capabilities_granted`, :func:`approval_required`), always
  requires ``evidence``, and appends one
  ``app_shared.models.domain_playbooks.DomainLifecycleAudit`` row per
  call — never mutates an existing one (the audit table is append-only
  at the database level too, via a trigger; see that model's docstring).
* :func:`unsupported_target_outcome` — the product-visible, zero-retry
  outcome contract for ``DomainState.UNSUPPORTED`` (see its own
  docstring for why this is a contract function and not a direct
  ``mark_target`` call from here).

The approval gate: derived from the rule table, never restated
--------------------------------------------------------------
W4's first cut gated approval on ``to_state is DomainState.ACTIVE``
alone. The W4 gate review (2026-08-26) found that this was a **one-hop
approval bypass**: C3's rule table
(``app_shared.domains.state_lookup.authorization_rules_for_state``)
grants ``DIRECT_CANARY`` permissions that are byte-identical to
``ACTIVE``'s — ``paid_allowed``/``broad_crawl_allowed``/
``expensive_escalation_allowed`` all ``True`` — and ``UNKNOWN ->
DIRECT_CANARY`` required no approver at all. An operator could therefore
reach full C3 authorization for an uncertified domain in a single
unapproved move, without ever entering ``ACTIVE``.

The gate is now computed **from the rule table itself** rather than
restated as a hand-maintained list of "grant edges":
:func:`capabilities_granted` calls ``authorization_rules_for_state``
(read-only) for both endpoints of the edge and reports every gate the
destination turns ``True`` that the source has ``False``. Any non-empty
result requires a recorded human ``approver``. Consequences:

* **Capability-granting** (approval required): ``UNKNOWN ->
  DIRECT_CANARY`` (grants broad crawl + expensive escalation),
  ``DEGRADED -> ACTIVE``/``DEGRADED -> PROFILE_CANARY`` (grant expensive
  escalation), ``QUARANTINED -> UNKNOWN``/``UNSUPPORTED -> UNKNOWN``
  (grant paid work — releasing a quarantine IS a grant).
* **Capability-neutral or -reducing** (no approver needed, so automatic
  demotions still work with no human in the loop): ``ACTIVE ->
  DEGRADED``, anything ``-> QUARANTINED``/``-> UNSUPPORTED``,
  ``DIRECT_CANARY -> PROFILE_CANARY``, ``DIRECT_CANARY -> UNKNOWN``.

Because the table makes ``PROFILE_CANARY`` and ``ACTIVE`` byte-identical,
the table-derived rule alone would *stop* requiring approval for
``PROFILE_CANARY -> ACTIVE`` — the certification edge. So the gate is the
**union** of the two readings: table-derived capability grants **or**
``to_state is DomainState.ACTIVE``. Union, not replacement, is the
conservative direction (it can only ever require *more* approvals than
either reading alone), and it keeps full certification an explicitly
human act even if a future rule-table edit made some other state look
equally permissive. Widening the rule table therefore automatically
widens this gate; it can never silently narrow it below "-> ACTIVE".

**Recertification from DEGRADED**: modeled as ``DEGRADED -> ACTIVE``
(direct recertification) and ``DEGRADED -> PROFILE_CANARY`` (re-canary
before restoring full certification). Both are legal edges in
:data:`ALLOWED_TRANSITIONS` and both now require an approver — leaving
``DEGRADED`` restores ``expensive_escalation_allowed``, which is exactly
the capability ``DEGRADED`` exists to withhold.

``profile_fields`` is owner-populated
-------------------------------------
:func:`transition` never invents, validates, or defaults any key inside
``DomainPlaybook.profile_fields`` — that JSONB blob is populated by the
domain's accountable owner (``profile_owner``) out of band, and this
module treats it as opaque. The columns this module *does* write are
``state``, ``profile_version``, ``last_canary_at`` (canary edges only)
and, when explicitly passed, ``profile_owner``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select

from app_shared.domains.state_lookup import authorization_rules_for_state
from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.models.domain_playbooks import DomainLifecycleAudit, DomainPlaybook, DomainState

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANARY_STATES",
    "CAPABILITY_GATES",
    "ApprovalRequiredError",
    "InvalidTransitionError",
    "LifecycleError",
    "UNSUPPORTED_ERROR_CODE",
    "UNSUPPORTED_TARGET_STATUS",
    "approval_required",
    "capabilities_granted",
    "transition",
    "unsupported_target_outcome",
]


class LifecycleError(Exception):
    """Base class for every error :func:`transition` can raise."""


class InvalidTransitionError(LifecycleError):
    """Raised when ``(from_state, to_state)`` is not a legal edge in
    :data:`ALLOWED_TRANSITIONS`, or ``evidence`` is missing/empty."""


class ApprovalRequiredError(LifecycleError):
    """Raised when a **capability-granting** transition is attempted with
    ``approver=None``/``""``.

    "Capability-granting" is derived from C3's own rule table rather than
    restated here — see :func:`capabilities_granted` and this module's
    docstring for the gate-review finding (a one-hop approval bypass via
    ``UNKNOWN -> DIRECT_CANARY``) that widened this beyond
    ``-> ACTIVE``."""


# --------------------------------------------------------------------------
# 1. The lifecycle graph
# --------------------------------------------------------------------------

#: Legal ``(from_state -> {to_state, ...})`` edges. Anything not listed
#: here (including every self-loop — ``state -> state`` is never a
#: member of its own value set below) is rejected by :func:`transition`
#: with :class:`InvalidTransitionError`. Deliberately does not attempt
#: to encode every conceivable operational path — this is the
#: conservative W4.1 graph; widening it is additive (add an edge to a
#: set) and does not require touching :func:`transition` itself.
ALLOWED_TRANSITIONS: dict[DomainState, frozenset[DomainState]] = {
    DomainState.UNKNOWN: frozenset(
        {
            DomainState.DIRECT_CANARY,
            DomainState.UNSUPPORTED,
            DomainState.QUARANTINED,
        }
    ),
    DomainState.DIRECT_CANARY: frozenset(
        {
            DomainState.PROFILE_CANARY,
            DomainState.UNSUPPORTED,
            DomainState.QUARANTINED,
            DomainState.UNKNOWN,
        }
    ),
    DomainState.PROFILE_CANARY: frozenset(
        {
            DomainState.ACTIVE,
            DomainState.DIRECT_CANARY,
            DomainState.UNSUPPORTED,
            DomainState.QUARANTINED,
        }
    ),
    DomainState.ACTIVE: frozenset(
        {
            DomainState.DEGRADED,
            DomainState.QUARANTINED,
            DomainState.UNSUPPORTED,
        }
    ),
    DomainState.DEGRADED: frozenset(
        {
            DomainState.ACTIVE,  # recertification
            DomainState.PROFILE_CANARY,  # re-canary before restoring
            DomainState.QUARANTINED,
            DomainState.UNSUPPORTED,
        }
    ),
    DomainState.QUARANTINED: frozenset(
        {
            DomainState.UNKNOWN,
            DomainState.UNSUPPORTED,
        }
    ),
    DomainState.UNSUPPORTED: frozenset(
        {
            DomainState.UNKNOWN,  # a later re-evaluation may reconsider
        }
    ),
}


# --------------------------------------------------------------------------
# 2. The approval gate, derived from C3's rule table
# --------------------------------------------------------------------------

#: The field names on
#: ``app_shared.domains.state_lookup.DomainAuthorizationRules`` that
#: represent a *capability C3 grants*. Read off the dataclass at import
#: time rather than hand-listed, so a gate added to that table is picked
#: up by :func:`capabilities_granted` automatically instead of silently
#: falling outside the approval gate — the drift this ordering exists to
#: prevent (W4 gate review, 2026-08-26).
CAPABILITY_GATES: tuple[str, ...] = tuple(
    authorization_rules_for_state(DomainState.ACTIVE).__dataclass_fields__
)

#: States whose *entry* is a canary run against the domain — the edges
#: that stamp ``DomainPlaybook.last_canary_at``.
CANARY_STATES: frozenset[DomainState] = frozenset(
    {DomainState.DIRECT_CANARY, DomainState.PROFILE_CANARY}
)


def capabilities_granted(
    from_state: DomainState, to_state: DomainState
) -> tuple[str, ...]:
    """Names of the C3 authorization gates ``to_state`` grants that
    ``from_state`` denies — i.e. what this edge *hands out*.

    Computed by CALLING
    ``app_shared.domains.state_lookup.authorization_rules_for_state`` for
    both endpoints (read-only; this module never copies, mirrors, or
    re-derives that table's contents), so the approval gate can never
    drift from the rule table C3 actually enforces. Empty tuple = this
    edge is capability-neutral or capability-reducing.
    """
    source = authorization_rules_for_state(from_state)
    destination = authorization_rules_for_state(to_state)
    return tuple(
        gate
        for gate in CAPABILITY_GATES
        if getattr(destination, gate) and not getattr(source, gate)
    )


def approval_required(from_state: DomainState, to_state: DomainState) -> bool:
    """Does this edge need a recorded human ``approver``?

    ``True`` when the edge grants any capability the source state denied
    (:func:`capabilities_granted`) **or** when it moves the domain into
    ``DomainState.ACTIVE`` (full certification stays an explicitly human
    act regardless of what the rule table happens to say — see this
    module's docstring for why the gate is the union of the two
    readings, never just one).
    """
    return to_state is DomainState.ACTIVE or bool(
        capabilities_granted(from_state, to_state)
    )


# --------------------------------------------------------------------------
# 3. transition()
# --------------------------------------------------------------------------


class _ScalarResult(Protocol):
    def scalar_one_or_none(self) -> DomainPlaybook | None: ...


class _SessionLike(Protocol):
    """The minimal ``Session`` surface :func:`transition` needs — real
    ``sqlalchemy.orm.Session`` satisfies this trivially; unit tests use a
    tiny stub (the same pattern ``tests/unit/domains/test_state_lookup.py``
    established for :func:`app_shared.domains.state_lookup.get_domain_state`),
    since this module's correctness is graph/approval/audit logic, not
    SQL — the actual DB round-trip is proven separately by the scratch-DB
    migration up/down/up (task contract, Step 5)."""

    def execute(self, stmt) -> _ScalarResult: ...

    def add(self, instance) -> None: ...


def transition(
    session: _SessionLike,
    domain: str,
    to_state: DomainState,
    *,
    evidence: dict,
    approver: str | None = None,
    profile_owner: str | None = None,
    now: datetime | None = None,
) -> DomainLifecycleAudit:
    """Move ``domain``'s ``domain_playbooks.state`` to ``to_state``.

    The single writer of ``domain_playbooks.state`` beyond C2's seed
    ``UPDATE`` (migration ``d9a46612bcc8``) — every call:

    1. Looks up the domain's current ``DomainPlaybook`` row. Raises
       :class:`InvalidTransitionError` if there is none (a transition
       needs something to transition *from*; a domain with no playbook
       row has no certification state to move — see
       ``state_lookup.get_domain_state``'s ``UNKNOWN`` default for the
       *read*-side equivalent of "no row").
    2. Requires non-empty ``evidence`` — raises
       :class:`InvalidTransitionError` otherwise. No exceptions: the
       task contract states evidence is mandatory for every transition.
    3. Checks ``(from_state, to_state)`` against :data:`ALLOWED_TRANSITIONS`
       — raises :class:`InvalidTransitionError` for an illegal edge
       (including a self-loop, which is never a legal edge here).
    4. Requires ``approver`` (raises :class:`ApprovalRequiredError`
       otherwise) for every capability-granting edge —
       :func:`approval_required`, which derives "granting" by calling
       C3's own ``authorization_rules_for_state`` rather than restating
       it. See this module's docstring for the gate-review finding that
       widened this beyond ``-> ACTIVE``.
    5. Mutates the ``DomainPlaybook`` row's ``state`` and increments its
       ``profile_version`` (this transition IS the new certified
       profile version — see ``DomainPlaybook.profile_version``'s
       docstring). On an edge INTO a canary state
       (:data:`CANARY_STATES`) it also stamps ``last_canary_at`` — the
       transition is what starts the canary, so the timestamp lands on
       exactly the same commit as the state that makes the canary legal,
       and (per that column's own docstring) records that a canary *ran*,
       not that it passed. When ``profile_owner`` is passed it is
       recorded onto the playbook row too; passing ``None`` (the
       default) leaves any existing owner untouched — an ownership
       change is an explicit act, never an accident of omitting a kwarg.
    6. Builds and ``session.add()``s exactly one
       :class:`~app_shared.models.domain_playbooks.DomainLifecycleAudit`
       row capturing the edge, evidence, approver, and the new
       ``profile_version`` — then returns it. Does not commit; that is
       the caller's transaction to manage, same convention as
       ``app_shared.jobs.targets.mark_target``.

    For a domain whose current state was seeded directly (C2's
    ``amazon.sa``/``noon.com``/``stech.ink`` -> ``ACTIVE`` ``UPDATE``,
    with no prior audit row), the row this call produces is simply the
    FIRST audit row for that domain — ``from_state`` is still correctly
    populated from the seeded value, there is just nothing before it.
    That is documented, expected behavior, not a gap (see
    ``DomainLifecycleAudit``'s class docstring).

    ``now`` is an injectable clock (defaults to ``datetime.now(utc)``)
    purely so the ``last_canary_at`` stamp is deterministic in tests —
    mirrors ``state_lookup.get_domain_state``'s ``monotonic`` parameter.
    """
    if not evidence:
        raise InvalidTransitionError(
            "evidence is required for every domain lifecycle transition "
            f"(domain={domain!r}, to_state={to_state.value})"
        )

    playbook = session.execute(
        select(DomainPlaybook).where(DomainPlaybook.domain == domain)
    ).scalar_one_or_none()
    if playbook is None:
        raise InvalidTransitionError(
            f"no domain_playbooks row for domain={domain!r} -- nothing to transition"
        )

    from_state = playbook.state
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidTransitionError(
            f"{from_state.value} -> {to_state.value} is not an allowed domain "
            f"lifecycle transition (domain={domain!r})"
        )

    if approval_required(from_state, to_state) and not approver:
        granted = capabilities_granted(from_state, to_state)
        why = (
            f"grants {', '.join(granted)}"
            if granted
            else "is full certification (-> ACTIVE)"
        )
        raise ApprovalRequiredError(
            f"{from_state.value} -> {to_state.value} {why} and requires "
            f"a recorded human approver (domain={domain!r})"
        )

    playbook.state = to_state
    playbook.profile_version += 1
    if to_state in CANARY_STATES:
        playbook.last_canary_at = now or datetime.now(timezone.utc)
    if profile_owner:
        playbook.profile_owner = profile_owner

    audit_row = DomainLifecycleAudit(
        domain=domain,
        from_state=from_state.value,
        to_state=to_state.value,
        evidence=evidence,
        approver=approver,
        profile_version=playbook.profile_version,
    )
    session.add(audit_row)
    return audit_row


# --------------------------------------------------------------------------
# 4. UNSUPPORTED's product-visible, zero-retry outcome contract
# --------------------------------------------------------------------------

#: The terminal ``scrape_job_targets.status`` a consumer must pass to
#: ``app_shared.jobs.targets.mark_target`` when dispatch discovers (via
#: ``app_shared.domains.state_lookup.get_domain_state``) that a target's
#: domain is ``DomainState.UNSUPPORTED``. ``SKIPPED`` rather than
#: ``DEFERRED`` is what makes "zero retries" true as a structural
#: property rather than a policy someone has to remember: ``mark_target``
#: treats every member of its terminal-status set (which ``SKIPPED``
#: is and ``DEFERRED`` deliberately is not) as never-transitioned-again,
#: and the dispatch layer only ever re-picks ``PENDING``/``DEFERRED``
#: targets (``app_shared.jobs.targets`` module docstring) — so a target
#: marked ``SKIPPED`` for an ``UNSUPPORTED`` domain is never re-dispatched
#: by construction, not by a retry-count check this module would have to
#: invent and keep in sync.
UNSUPPORTED_TARGET_STATUS = ScrapeTargetStatus.SKIPPED

#: The ``scrape_job_targets.error_code`` paired with
#: :data:`UNSUPPORTED_TARGET_STATUS`. ``BLOCKED`` — not the more literal-
#: sounding ``NOT_LISTED`` — because ``NOT_LISTED`` is reserved
#: end-to-end (the scraping runtime's variant-resolution adapter,
#: ``IdentityStatus``) for "this specific PRODUCT was proven absent from
#: an otherwise-working store", a claim about catalog membership this
#: module has no evidence for. ``DomainState.UNSUPPORTED`` means "not
#: scrapeable by any supported METHOD" — a capability statement, which
#: ``BLOCKED`` (an existing, emitted ``ScrapeErrorCode`` member) already
#: represents without inventing a parallel outcome channel. This is a
#: documented, conservative reading (task contract: material ambiguity
#: -> conservative additive choice); a maintainer closer to the specific
#: product-surface wording is free to pick a different existing code.
UNSUPPORTED_ERROR_CODE = ScrapeErrorCode.BLOCKED


def unsupported_target_outcome() -> tuple[ScrapeTargetStatus, ScrapeErrorCode]:
    """The ``(status, error_code)`` pair a dispatch-path consumer should
    pass to ``app_shared.jobs.targets.mark_target`` for a target whose
    domain resolves to ``DomainState.UNSUPPORTED``.

    Pure and offline — no DB, no import of anything under ``apps/``
    (this package must stay importable without them, same boundary rule
    ``ScrapeProfile.price_json_path``'s docstring states for the
    scraping package). Wiring the actual dispatch-path check
    (``get_domain_state(...) is DomainState.UNSUPPORTED`` ->
    ``mark_target(..., *unsupported_target_outcome())`` instead of
    dispatching) touches ``apps/`` and is therefore reported as a
    one-line follow-up rather than implemented in this task (task
    contract: fenced-path wiring becomes a reported follow-up, not a
    fenced-path edit).
    """
    return UNSUPPORTED_TARGET_STATUS, UNSUPPORTED_ERROR_CODE
