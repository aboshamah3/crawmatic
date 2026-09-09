"""Pure resolution and circuit logic for versioned strategy-method chains.

The resolver operates on ORM rows but performs no I/O.  Keeping the decision
pure makes the same ordered chain usable by HTTP dispatch, browser dispatch,
workers, canaries, and API previews without embedding competitor names or
product assumptions in orchestration code.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

from app_shared.enums import (
    AccessMethod,
    ScrapeErrorCode,
    ScrapeProfileMode,
    StrategyMethodProofState,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_STRATEGY_VERSION",
    "AttemptBudgetGate",
    "CircuitPolicy",
    "LadderDecision",
    "MethodCircuitSnapshot",
    "PlaybookStrategy",
    "StrategyMethodSelection",
    "evolve_method_circuit",
    "is_method_health_failure",
    "mode_for_access_method",
    "resolve_method_candidate",
    "resolve_next_physical_attempt",
]

#: The strategy version an attempt carries when its domain has no
#: ``domain_playbooks`` row (or the row predates C4's columns). ``1``, not
#: ``0``/``None``: every attempt has *some* strategy, and "version 1" is
#: the honest name for "the ladder's own default order", which is exactly
#: what such an attempt ran under. A stamped ``None`` would force every
#: reader of ``request_attempts`` to handle a third case that means the
#: same thing as the first.
DEFAULT_STRATEGY_VERSION = 1


class StrategyMethodLike(Protocol):
    id: uuid.UUID
    access_method: AccessMethod
    priority: int
    enter_on: list
    fallback_on: list
    enabled: bool
    proof_state: StrategyMethodProofState
    cooldown_until: datetime | None
    next_canary_at: datetime | None
    retired_at: datetime | None


@dataclass(frozen=True)
class StrategyMethodSelection:
    method: StrategyMethodLike
    attempt_ordinal: int
    is_canary: bool = False
    #: The domain's ``domain_playbooks.strategy_version`` at the moment
    #: this attempt was resolved (EPA C4). Stamped on EVERY selection, so
    #: the attempt row the caller persists can say which strategy shape
    #: produced it — the question a regressed canary has to answer.
    #: :data:`DEFAULT_STRATEGY_VERSION` when the domain has no playbook.
    strategy_version: int = DEFAULT_STRATEGY_VERSION
    #: Whether the selected method is the playbook's rationed
    #: ``fallback_path`` (the expensive rung). ``True`` means this
    #: attempt has already been charged against
    #: ``fallback_cap_per_refresh`` for its target.
    is_fallback_path: bool = False


class AttemptBudgetGate(Protocol):
    """The per-target physical-attempt gate the ladder consults (EPA C1/F08).

    Structural, not an import: the concrete implementation is
    the scraping runtime's ``AttemptBudget``, and ``app_shared`` is
    the LOWER layer — it may not import the scraping runtime (Constitution
    I/V, ``tests/unit/test_import_boundaries.py``). Typing the gate as a
    Protocol keeps this resolver free of I/O *and* of that dependency
    while still making "consult the budget before every physical attempt"
    a property of the ladder itself rather than of each call site's
    discipline.
    """

    def is_suppressed(self, method: Any) -> bool: ...

    def try_consume(self, method: Any) -> Any: ...


#: Names of the two OPTIONAL gate methods the fallback cap needs. They are
#: read with ``getattr`` and only when a playbook actually caps the
#: fallback path, so every gate written before C4 (including C1's own unit
#: fakes) keeps working unchanged — an uncapped domain never asks.
_FALLBACKS_USED = "fallback_attempts_used"
_CHARGE_FALLBACK = "charge_fallback_attempt"


@dataclass(frozen=True)
class PlaybookStrategy:
    """The ladder-relevant half of one ``domain_playbooks`` row (EPA C4).

    A plain value object, not the ORM row: ``methods`` is the pure
    resolver and must stay importable without SQLAlchemy, so
    :meth:`from_row` reads whatever it is handed **structurally**
    (``getattr``) — a ``DomainPlaybook``, a stub, or a dict-like shim all
    work, and a row that predates C4's migration simply yields the
    defaults.

    Both paths are validated against :class:`~app_shared.enums.AccessMethod`
    here rather than at the column: the columns are curated operator data
    on a fleet-wide reference table, so an unrecognised value must degrade
    to "no hint" (and say so in a log the operator can find) instead of
    failing a seed INSERT or, worse, silently matching nothing while
    looking configured.
    """

    strategy_version: int = DEFAULT_STRATEGY_VERSION
    cheap_path: AccessMethod | None = None
    fallback_path: AccessMethod | None = None
    #: ``None`` = uncapped (pre-C4 behaviour). ``0`` = the fallback path
    #: is never taken. Never negative — :meth:`from_row` clamps.
    fallback_cap_per_refresh: int | None = None
    #: ``None`` = use ``Settings.SCRAPE_RECOVERY_PROBE_FRACTION``. Not read
    #: by the ladder itself (the ladder never samples); carried here so
    #: the one place that loads the playbook can hand it to the budget it
    #: constructs, instead of every call site loading the row twice.
    recovery_probe_fraction: float | None = None

    @classmethod
    def from_row(cls, row: Any) -> "PlaybookStrategy | None":
        """Build from a playbook-shaped object; ``None`` in, ``None`` out.

        ``None`` out means "this domain has no playbook", which the ladder
        treats as "no strategy hints" — deliberately the same as a
        playbook with every C4 column unset, because those are the same
        situation from the ladder's point of view.
        """
        if row is None:
            return None
        cap = getattr(row, "fallback_cap_per_refresh", None)
        fraction = getattr(row, "recovery_probe_fraction", None)
        return cls(
            strategy_version=int(
                getattr(row, "strategy_version", None) or DEFAULT_STRATEGY_VERSION
            ),
            cheap_path=_as_access_method(getattr(row, "cheap_path", None), row=row),
            fallback_path=_as_access_method(
                getattr(row, "fallback_path", None), row=row
            ),
            fallback_cap_per_refresh=None if cap is None else max(0, int(cap)),
            recovery_probe_fraction=None if fraction is None else float(fraction),
        )

    def is_cheap_path(self, access_method: AccessMethod) -> bool:
        return self.cheap_path is not None and access_method == self.cheap_path

    def is_fallback_path(self, access_method: AccessMethod) -> bool:
        return self.fallback_path is not None and access_method == self.fallback_path

    def caps_fallback(self) -> bool:
        """Whether this playbook rations its fallback path at all."""
        return self.fallback_path is not None and self.fallback_cap_per_refresh is not None


def _as_access_method(value: Any, *, row: Any = None) -> AccessMethod | None:
    """Coerce a curated ``cheap_path``/``fallback_path`` string, or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, AccessMethod):
        return value
    try:
        return AccessMethod(str(value))
    except ValueError:
        logger.warning(
            "strategy.methods: ignoring unknown access method %r on playbook %r",
            value,
            getattr(row, "domain", None),
        )
        return None


@dataclass(frozen=True)
class LadderDecision:
    """One ladder answer: a method to run, or why nothing may run.

    Exactly one of the two is ever set. ``refusal`` carries a §34 code
    that is TERMINAL for the whole target (``ATTEMPT_BUDGET_EXHAUSTED``,
    ``TARGET_DEADLINE_EXCEEDED``) — a merely *suppressed* method is not a
    refusal, it just moves the cursor to the next rung, so it surfaces as
    ``selection=None, refusal=None`` exactly like "the chain ended",
    which is the same thing from the caller's point of view.
    """

    selection: StrategyMethodSelection | None = None
    refusal: ScrapeErrorCode | None = None
    #: The domain's ``domain_playbooks.strategy_version`` this decision was
    #: taken under (EPA C4) — stamped on refusals and dead ends too, not
    #: only on selections, so "version 7 refused every target" is as
    #: answerable as "version 7 produced these prices".
    strategy_version: int = DEFAULT_STRATEGY_VERSION


def mode_for_access_method(access_method: AccessMethod) -> ScrapeProfileMode:
    """Derive node mode from the selected candidate's actual transport."""
    if access_method in (
        AccessMethod.PLAYWRIGHT_DIRECT,
        AccessMethod.PLAYWRIGHT_PROXY,
    ):
        return ScrapeProfileMode.BROWSER
    return ScrapeProfileMode.HTTP


_PERMANENT_OUTCOMES = frozenset(
    {
        ScrapeErrorCode.NOT_LISTED,
        ScrapeErrorCode.IDENTITY_MISMATCH,
        ScrapeErrorCode.POLICY_BLOCKED,
    }
)
_PROXY_METHODS = frozenset(
    {AccessMethod.PROXY_HTTP, AccessMethod.PLAYWRIGHT_PROXY}
)


def _fallback_matches(method: StrategyMethodLike, outcome: ScrapeErrorCode) -> bool:
    configured = {
        value.value if isinstance(value, ScrapeErrorCode) else str(value)
        for value in (method.fallback_on or [])
    }
    return "*" in configured or outcome.value in configured


def _entry_matches(method: StrategyMethodLike, outcome: ScrapeErrorCode) -> bool:
    configured = {
        value.value if isinstance(value, ScrapeErrorCode) else str(value)
        for value in (getattr(method, "enter_on", None) or [])
    }
    return not configured or "*" in configured or outcome.value in configured


def _is_eligible(
    method: StrategyMethodLike,
    *,
    now: datetime,
    open_method_ids: frozenset[uuid.UUID],
    proxy_budget_exhausted: bool,
) -> tuple[bool, bool]:
    if not method.enabled or method.retired_at is not None:
        return False, False
    if method.proof_state is StrategyMethodProofState.DISABLED:
        return False, False
    if proxy_budget_exhausted and method.access_method in _PROXY_METHODS:
        return False, False

    canary_due = method.next_canary_at is not None and method.next_canary_at <= now
    if method.proof_state is StrategyMethodProofState.QUARANTINED:
        return canary_due, canary_due
    if method.id in open_method_ids:
        return canary_due, canary_due
    if method.cooldown_until is not None and method.cooldown_until > now:
        return canary_due, canary_due
    return True, False


def resolve_method_candidate(
    methods: Iterable[StrategyMethodLike],
    *,
    preferred_method_id: uuid.UUID | None = None,
    current_method_id: uuid.UUID | None = None,
    outcome: ScrapeErrorCode | None = None,
    current_attempt_ordinal: int = 0,
    now: datetime | None = None,
    open_method_ids: frozenset[uuid.UUID] = frozenset(),
    proxy_budget_exhausted: bool = False,
    budget: AttemptBudgetGate | None = None,
    playbook: PlaybookStrategy | None = None,
) -> StrategyMethodSelection | None:
    """Select the first runnable method after an outcome-conditioned cursor.

    A permanent policy/identity/not-listed outcome ends the chain.  Other
    outcomes only advance when the current method explicitly lists them in
    ``fallback_on`` (or ``*``), ensuring configuration—not customer-specific
    code—owns escalation behavior.

    ``budget`` (EPA C1/F08) is the per-target physical-attempt gate. When
    supplied it is consulted for EVERY candidate that survives the
    circuit/eligibility filter, *before* that candidate is returned to be
    fetched: a suppressed method is skipped, and an exhausted budget or a
    passed target deadline ends the chain outright. Returns ``None`` for
    both of those, which is why callers that must persist *why* nothing
    ran use :func:`resolve_next_physical_attempt` instead — this function
    keeps its historical ``StrategyMethodSelection | None`` shape so no
    existing caller changes.
    """
    return resolve_next_physical_attempt(
        methods,
        preferred_method_id=preferred_method_id,
        current_method_id=current_method_id,
        outcome=outcome,
        current_attempt_ordinal=current_attempt_ordinal,
        now=now,
        open_method_ids=open_method_ids,
        proxy_budget_exhausted=proxy_budget_exhausted,
        budget=budget,
        playbook=playbook,
    ).selection


def resolve_next_physical_attempt(
    methods: Iterable[StrategyMethodLike],
    *,
    preferred_method_id: uuid.UUID | None = None,
    current_method_id: uuid.UUID | None = None,
    outcome: ScrapeErrorCode | None = None,
    current_attempt_ordinal: int = 0,
    now: datetime | None = None,
    open_method_ids: frozenset[uuid.UUID] = frozenset(),
    proxy_budget_exhausted: bool = False,
    budget: AttemptBudgetGate | None = None,
    playbook: PlaybookStrategy | None = None,
) -> LadderDecision:
    """:func:`resolve_method_candidate`, but saying WHY when nothing runs.

    The escalation ladder's single chokepoint (EPA C1/F08). Every
    physical attempt in the system is a method this function returned, so
    consulting ``budget`` here — rather than at each of the three call
    sites that fetch — is what makes "the budget is checked before every
    physical attempt" structurally true instead of a convention.

    Charging happens here too: a returned ``selection`` has ALREADY been
    charged one physical attempt via ``budget.try_consume``. That is
    deliberate — a caller that resolved a method and then failed to fetch
    still consumed the intent, and a budget that is only charged on
    success cannot bound a method that reliably crashes.

    ``playbook`` (EPA C4) is this domain's versioned strategy, read from
    ``domain_playbooks`` by the caller and passed in as a value object
    (this module stays pure). It does three things and nothing else:

    * every returned decision — selection, refusal, or dead end — is
      stamped with its ``strategy_version``;
    * ``cheap_path`` moves the near-free rung to the front when nothing
      else pins the start (no durable cursor, no preferred method), so a
      domain does not pay for an expensive first attempt it never needed;
    * ``fallback_path`` + ``fallback_cap_per_refresh`` ration the
      expensive rung to N attempts per target per refresh. The count
      lives on ``budget`` (the only object that is per-target and
      per-refresh), reached through the two OPTIONAL methods named by
      :data:`_FALLBACKS_USED`/:data:`_CHARGE_FALLBACK`. A gate that does
      not implement them reports zero used and cannot be charged — which
      makes the cap inert rather than wrong, and is why an uncapped
      domain never asks. Hitting the cap is NOT terminal for the target:
      it skips that rung, exactly like a suppression.
    """
    now = now or datetime.now(timezone.utc)
    strategy_version = (
        playbook.strategy_version if playbook is not None else DEFAULT_STRATEGY_VERSION
    )
    ordered = sorted(methods, key=lambda method: (method.priority, str(method.id)))
    if not ordered:
        return LadderDecision(strategy_version=strategy_version)

    if current_method_id is None:
        if preferred_method_id is not None:
            ordered.sort(
                key=lambda method: (
                    method.id != preferred_method_id,
                    method.priority,
                    str(method.id),
                )
            )
        elif playbook is not None and playbook.cheap_path is not None:
            # Only when NOTHING else pins the start. A durable cursor and
            # an explicit preferred method are both stronger statements
            # about where this target should begin than a domain-wide
            # hint, and the playbook has always been "a starting hint,
            # never an override" (see `app_shared.models.domain_playbooks`).
            ordered.sort(
                key=lambda method: (
                    not playbook.is_cheap_path(method.access_method),
                    method.priority,
                    str(method.id),
                )
            )
        candidates = ordered
    else:
        current_index = next(
            (index for index, method in enumerate(ordered) if method.id == current_method_id),
            None,
        )
        # A stale/unknown cursor must fail closed; restarting at priority 1
        # would duplicate paid work after a configuration revision.
        if current_index is None or outcome is None or outcome in _PERMANENT_OUTCOMES:
            return LadderDecision(strategy_version=strategy_version)
        current = ordered[current_index]
        if not _fallback_matches(current, outcome):
            return LadderDecision(strategy_version=strategy_version)
        candidates = ordered[current_index + 1 :]

    caps_fallback = playbook is not None and playbook.caps_fallback()
    for method in candidates:
        if outcome is not None and not _entry_matches(method, outcome):
            continue
        eligible, is_canary = _is_eligible(
            method,
            now=now,
            open_method_ids=open_method_ids,
            proxy_budget_exhausted=proxy_budget_exhausted,
        )
        if not eligible:
            continue
        is_fallback = playbook is not None and playbook.is_fallback_path(
            method.access_method
        )
        if caps_fallback and is_fallback:
            # Checked BEFORE `try_consume`: a rung this target may not
            # take again must cost nothing, exactly like a suppression.
            # Not terminal — a cheaper rung further down the ladder may
            # still be allowed to run.
            assert playbook is not None  # narrowed by `caps_fallback`
            cap = playbook.fallback_cap_per_refresh or 0
            if _fallback_attempts_used(budget) >= cap:
                continue
        if budget is not None:
            # Suppression is checked first and separately from charging:
            # a method this target already proved cannot read this page
            # must cost nothing at all, not one charged attempt.
            if budget.is_suppressed(method.access_method):
                continue
            verdict = budget.try_consume(method.access_method)
            error_code = getattr(verdict, "error_code", None)
            if error_code is not None:
                # Terminal for the TARGET (budget spent / deadline past)
                # -- no later rung can help, so stop the chain here
                # rather than charging the rest of the ladder for the
                # same refusal.
                return LadderDecision(
                    refusal=error_code, strategy_version=strategy_version
                )
            if not getattr(verdict, "allowed", True):
                # A non-terminal refusal (a suppression the gate itself
                # decided) -- try the next rung.
                continue
        if caps_fallback and is_fallback:
            # Charged only once the attempt is really happening -- after
            # the physical-attempt budget allowed it, never before, so a
            # deadline/exhaustion refusal cannot burn a fallback slot the
            # target never got to use.
            _charge_fallback_attempt(budget)
        return LadderDecision(
            selection=StrategyMethodSelection(
                method=method,
                attempt_ordinal=current_attempt_ordinal + 1,
                is_canary=is_canary,
                strategy_version=strategy_version,
                is_fallback_path=is_fallback,
            ),
            strategy_version=strategy_version,
        )
    return LadderDecision(strategy_version=strategy_version)


def _fallback_attempts_used(budget: AttemptBudgetGate | None) -> int:
    """How many rationed fallback attempts this target has already made.

    A gate that cannot answer (no budget at all, or one predating C4)
    reports ``0``, which makes the cap inert rather than wrong: refusing
    on an unknown count would silently disable the fallback path for
    every caller that has not adopted the counter yet.
    """
    reader = getattr(budget, _FALLBACKS_USED, None)
    if reader is None:
        return 0
    try:
        return int(reader())
    except Exception:  # noqa: BLE001 - a gate outage must not fail the ladder
        logger.warning(
            "strategy.methods: fallback-attempt count unavailable; treating as 0",
            exc_info=True,
        )
        return 0


def _charge_fallback_attempt(budget: AttemptBudgetGate | None) -> None:
    """Record one use of the rationed fallback path; best effort."""
    charger = getattr(budget, _CHARGE_FALLBACK, None)
    if charger is None:
        return
    try:
        charger()
    except Exception:  # noqa: BLE001 - see `_fallback_attempts_used`
        logger.warning(
            "strategy.methods: could not charge a fallback attempt", exc_info=True
        )


@dataclass(frozen=True)
class CircuitPolicy:
    failure_streak: int = 3
    minimum_attempts: int = 10
    failure_rate: float = 0.8
    cooldown: timedelta = timedelta(minutes=30)
    canary_interval: timedelta = timedelta(minutes=10)


@dataclass(frozen=True)
class MethodCircuitSnapshot:
    attempts: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    quarantined: bool = False
    cooldown_until: datetime | None = None
    next_canary_at: datetime | None = None


_OPERATIONAL_FAILURES = frozenset(
    {
        ScrapeErrorCode.PRICE_NOT_FOUND,
        ScrapeErrorCode.SELECTOR_BROKEN,
        ScrapeErrorCode.HTTP_403,
        ScrapeErrorCode.HTTP_429,
        ScrapeErrorCode.BLOCKED,
        ScrapeErrorCode.TIMEOUT,
        ScrapeErrorCode.PROXY_FAILED,
        ScrapeErrorCode.CONNECTION_FAILED,
        ScrapeErrorCode.TLS_CONNECTION_FAILED,
        ScrapeErrorCode.TLS_VERIFICATION_FAILED,
        ScrapeErrorCode.PROTOCOL_FAILED,
        ScrapeErrorCode.PLAYWRIGHT_FAILED,
    }
)


def is_method_health_failure(outcome: ScrapeErrorCode | None) -> bool:
    """Whether an outcome is evidence that this method—not the listing—is unhealthy."""
    return outcome in _OPERATIONAL_FAILURES


def evolve_method_circuit(
    snapshot: MethodCircuitSnapshot,
    *,
    outcome: ScrapeErrorCode | None,
    success: bool,
    policy: CircuitPolicy = CircuitPolicy(),
    now: datetime | None = None,
) -> MethodCircuitSnapshot:
    """Advance one `(domain, strategy_method)` breaker snapshot.

    Permanent catalog/policy outcomes are deliberately not method-health
    failures.  A successful canary closes its method circuit without
    affecting any other method for the same domain.
    """
    now = now or datetime.now(timezone.utc)
    attempts = snapshot.attempts + 1
    if success:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=snapshot.failures,
            consecutive_failures=0,
            quarantined=False,
        )
    if outcome not in _OPERATIONAL_FAILURES:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=snapshot.failures,
            consecutive_failures=0,
            quarantined=snapshot.quarantined,
            cooldown_until=snapshot.cooldown_until,
            next_canary_at=snapshot.next_canary_at,
        )

    failures = snapshot.failures + 1
    streak = snapshot.consecutive_failures + 1
    rate = failures / attempts
    should_open = streak >= policy.failure_streak or (
        attempts >= policy.minimum_attempts and rate >= policy.failure_rate
    )
    if should_open:
        return MethodCircuitSnapshot(
            attempts=attempts,
            failures=failures,
            consecutive_failures=streak,
            quarantined=True,
            cooldown_until=now + policy.cooldown,
            next_canary_at=now + policy.canary_interval,
        )
    return MethodCircuitSnapshot(
        attempts=attempts,
        failures=failures,
        consecutive_failures=streak,
        quarantined=snapshot.quarantined,
        cooldown_until=snapshot.cooldown_until,
        next_canary_at=snapshot.next_canary_at,
    )
