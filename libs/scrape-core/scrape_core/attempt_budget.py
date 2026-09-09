"""Per-target PHYSICAL attempt budget, deadline and method suppression (EPA C1/F08).

Three bounds already exist and none of them bounds what ONE target may
spend on fetches:

* ``SCRAPE_JOB_MAX_RUNTIME_SECONDS`` bounds the whole **job** (12 h).
* ``SCRAPE_MAX_DEFER_CYCLES`` bounds the **defer** loop, i.e. how many
  times a rate-limited target may be handed back — a target that is
  never rate-limited never touches it.
* ``FLEET_*``/access-policy limits bound the **rate**, not the total.

So a single target that escalates ``DIRECT_HTTP`` → impersonating HTTP →
``PROXY_HTTP`` → ``PLAYWRIGHT_PROXY``, retries each of those, and then
does it all again after a defer, pays for every one of those fetches and
nothing ever says stop. The deep dive found targets doing exactly that
for a whole job window, which is where "attempts per valid fresh price"
(A5's ``crawmatic_attempts_per_valid_fresh_24h``) goes wrong: the money
is spent on attempts that were never going to produce a price.

This module is the bound, and it holds three distinct facts:

``deadline``
    Wall clock. Past ``deadline_at`` the target is finalized
    ``TARGET_DEADLINE_EXCEEDED`` **without a fetch**. Pure arithmetic —
    no Redis, no I/O — so this bound holds even when everything else is
    unavailable.

``physical attempts``
    A counter in Redis keyed ``budget:{job_id}:{match_id}``, incremented
    once per PHYSICAL attempt (one HTTP request or one browser
    navigation; a retry counts again — a logical attempt that never
    reached the wire must never be charged). Past ``max_physical`` the
    ladder is refused ``ATTEMPT_BUDGET_EXHAUSTED``.

``method suppression``
    A method that already failed on **this target in this refresh** in a
    way that says "this method cannot read this page" (an
    ``EXTRACTION_FAILED`` 200, a ``SELECTOR_BROKEN``) is not tried again
    for this target. That is a *fact about this fetch* and is never
    probed. A **domain-level** suppression is a standing rule instead,
    and a standing rule that is never re-tested is permanent by
    construction — the cheap method can never produce the success that
    would lift it. So ``SCRAPE_RECOVERY_PROBE_FRACTION`` (5 %) of targets
    ignore a domain-level suppression and probe anyway, deterministically
    sampled per ``(match, method)`` so one target's answer is stable
    within a refresh.

**Redis failure is fail-OPEN**, same as ``scrape_core.defer_budget`` and
for a stronger reason here: every verdict this module produces is
*persisted evidence* about the target (``ATTEMPT_BUDGET_EXHAUSTED`` /
``TARGET_DEADLINE_EXCEEDED`` land in ``request_attempts.error_code`` and
``scrape_job_targets``). Fabricating that evidence out of a Redis outage
would poison the strategy optimizer and the domain scorecard for every
target at once, which is far worse than one extra fetch. The one bound
that does NOT depend on Redis — the deadline — is also the one that
actually caps the spend, so failing open never removes the ceiling.

Pure stdlib + ``app_shared.enums`` (Constitution I/V): no Scrapy, no
Twisted, no SQLAlchemy, so it is unit-testable off-reactor and importable
from the spider, the dispatcher and the workers alike.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from app_shared.enums import AccessMethod, ScrapeErrorCode

logger = logging.getLogger(__name__)

__all__ = [
    "AttemptBudget",
    "SuppressionScope",
    "Verdict",
    "attempt_budget_key",
    "domain_suppression_key",
    "fallback_attempts_key",
    "method_key",
    "target_suppression_key",
]

#: Default TTL for every key this module writes. Comfortably longer than
#: a job's lifetime (``SCRAPE_JOB_MAX_RUNTIME_SECONDS`` is 12 h) — the
#: counter and the per-target suppression set are job-scoped, so they
#: only need to outlive the job that owns them. Callers that know the
#: job's own TTL pass it as ``ttl_seconds``.
DEFAULT_TTL_SECONDS = 86_400

#: Domain-level suppression is NOT job-scoped — it is a standing rule
#: about a domain, so it deliberately outlives any one refresh. Bounded
#: anyway (never permanent) so a domain that quietly recovers is not
#: suppressed forever by a rule nobody remembers setting; the 5 % probe
#: is the other half of that guarantee.
DEFAULT_DOMAIN_SUPPRESSION_TTL_SECONDS = 21_600  # 6 h


class Verdict(StrEnum):
    """What the budget says about ONE prospective physical attempt."""

    #: Go ahead — the attempt has been charged to the budget.
    ALLOWED = "ALLOWED"
    #: This METHOD may not be used for this target; try the next rung of
    #: the ladder. Not terminal, and nothing was charged.
    SUPPRESSED = "SUPPRESSED"
    #: The target has spent every physical attempt it gets this refresh.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: The target ran past its own wall-clock deadline.
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"

    @property
    def allowed(self) -> bool:
        return self is Verdict.ALLOWED

    @property
    def terminal(self) -> bool:
        """Whether this verdict finalizes the TARGET (not just a method)."""
        return self in (Verdict.BUDGET_EXHAUSTED, Verdict.DEADLINE_EXCEEDED)

    @property
    def error_code(self) -> ScrapeErrorCode | None:
        """The §34 code to persist, or ``None`` when nothing is terminal."""
        if self is Verdict.BUDGET_EXHAUSTED:
            return ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED
        if self is Verdict.DEADLINE_EXCEEDED:
            return ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED
        return None


class SuppressionScope(StrEnum):
    """How wide a method suppression reaches.

    ``TARGET`` — this ``(job, match)`` only, for this refresh. A fact
    about one fetch; never probed, never carried forward.

    ``DOMAIN`` — every target on this domain, until the suppression's own
    TTL expires. A standing rule, and therefore falsifiable: a sampled
    ``SCRAPE_RECOVERY_PROBE_FRACTION`` of targets ignore it.
    """

    TARGET = "TARGET"
    DOMAIN = "DOMAIN"


#: Outcomes that mean "this METHOD cannot read this page", as opposed to
#: "this page has no price" or "the host pushed back". Only these justify
#: suppressing a method for the rest of the target's refresh: re-running
#: the same method against the same URL after one of them is guaranteed
#: to produce the same nothing, at full cost.
SUPPRESSING_OUTCOMES = frozenset(
    {
        ScrapeErrorCode.EXTRACTION_FAILED,
        ScrapeErrorCode.SELECTOR_BROKEN,
    }
)


def method_key(method: Any) -> str:
    """Normalize a method (enum, ORM row, or string) to a stable key.

    Accepts an :class:`~app_shared.enums.AccessMethod`, anything carrying
    an ``access_method`` attribute (a ``DomainStrategyMethod`` row, a
    ``StrategyMethodSelection``'s ``method``), or a bare string, so the
    ladder can hand this whatever it already has without unwrapping.
    """
    if isinstance(method, AccessMethod):
        return method.value
    access_method = getattr(method, "access_method", None)
    if access_method is not None:
        return method_key(access_method)
    return str(method)


def attempt_budget_key(
    job_id: uuid.UUID | str, match_id: uuid.UUID | str
) -> str:
    """Redis key holding this target's physical-attempt count."""
    return f"budget:{job_id}:{match_id}"


def target_suppression_key(
    job_id: uuid.UUID | str, match_id: uuid.UUID | str
) -> str:
    """Redis SET of methods suppressed for this target in this refresh."""
    return f"budget:{job_id}:{match_id}:suppressed"


def domain_suppression_key(domain: str) -> str:
    """Redis SET of methods suppressed fleet-wide for one domain."""
    return f"budgetsupp:domain:{domain.strip().lower()}"


def fallback_attempts_key(
    job_id: uuid.UUID | str, match_id: uuid.UUID | str
) -> str:
    """Redis key counting this target's uses of the rationed fallback path.

    Deliberately SEPARATE from :func:`attempt_budget_key` (EPA C4): the
    physical-attempt counter bounds how much a target may spend in total,
    while this one bounds how much of that spend may go to the ONE rung
    the playbook marks expensive (``domain_playbooks.fallback_path``,
    capped by ``fallback_cap_per_refresh``). Folding them together would
    make "3 cheap attempts" and "3 browser attempts" the same fact, which
    is precisely the distinction the cap exists to draw.
    """
    return f"budget:{job_id}:{match_id}:fallback"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _as_aware(moment: datetime) -> datetime:
    """Treat a naive deadline as UTC rather than raising mid-fetch."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


class AttemptBudget:
    """One target's physical-attempt budget, deadline and suppression set.

    Construct one per ``(job, match)`` for the life of that target's
    refresh and consult it before EVERY physical attempt — that is the
    invariant the escalation ladder
    (``app_shared.strategy.methods.resolve_next_physical_attempt``)
    enforces on its callers' behalf.

    Args:
        redis: Any ``redis.Redis``-shaped client (``incr``/``expire``/
            ``sadd``/``sismember``). Never awaited — the calls are the
            same blocking ones the rest of the scraping runtime makes
            off-reactor.
        job_id: The ``scrape_jobs`` row this refresh belongs to. The
            counter is job-scoped so the next refresh starts fresh.
        match_id: The ``competitor_product_matches`` row being priced.
        max_physical: ``Settings.SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS``.
            ``<= 0`` refuses everything (an explicit "spend nothing").
        deadline_at: Wall-clock instant past which the target is
            finalized without another fetch — normally
            ``target_started_at + SCRAPE_TARGET_DEADLINE_SECONDS``.
        domain: Bare competitor domain, needed only for DOMAIN-scope
            suppression. ``None`` disables domain suppression entirely
            (never silently treats it as a match-all).
        recovery_probe_fraction:
            ``Settings.SCRAPE_RECOVERY_PROBE_FRACTION``. Defaults to 0.0
            so a caller that forgets it gets the conservative behaviour
            (respect the suppression) rather than a surprise spend.
        ttl_seconds: The job's TTL for the job-scoped keys.
        now: Injectable clock for tests. Called per operation, never
            cached — a budget object outlives many attempts.
    """

    def __init__(
        self,
        redis: Any,
        *,
        job_id: uuid.UUID | str,
        match_id: uuid.UUID | str,
        max_physical: int,
        deadline_at: datetime,
        domain: str | None = None,
        recovery_probe_fraction: float = 0.0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        domain_ttl_seconds: int = DEFAULT_DOMAIN_SUPPRESSION_TTL_SECONDS,
        now: datetime | None = None,
    ) -> None:
        self._redis = redis
        self.job_id = job_id
        self.match_id = match_id
        self.max_physical = int(max_physical)
        self.deadline_at = _as_aware(deadline_at)
        self.domain = domain
        self.recovery_probe_fraction = float(recovery_probe_fraction)
        self.ttl_seconds = int(ttl_seconds)
        self.domain_ttl_seconds = int(domain_ttl_seconds)
        self._now_override = now

    # -- deadline ---------------------------------------------------------

    def deadline_exceeded(self, *, now: datetime | None = None) -> bool:
        """Whether this target is already past its wall-clock deadline.

        Pure arithmetic — the one bound that survives a Redis outage.
        """
        return _now(now or self._now_override) >= self.deadline_at

    # -- suppression ------------------------------------------------------

    def suppress(
        self,
        method: Any,
        *,
        scope: SuppressionScope = SuppressionScope.TARGET,
    ) -> None:
        """Stop using ``method`` for this target (or this domain).

        Best-effort: a Redis failure is logged and swallowed. A
        suppression we failed to record costs one redundant attempt,
        which the physical-attempt counter still bounds; raising here
        would turn a cache outage into a failed target.
        """
        key = method_key(method)
        if scope is SuppressionScope.DOMAIN:
            if not self.domain:
                logger.warning(
                    "attempt_budget: DOMAIN suppression of %s ignored -- no domain "
                    "on this budget (job=%s match=%s)",
                    key,
                    self.job_id,
                    self.match_id,
                )
                return
            redis_key = domain_suppression_key(self.domain)
            ttl = self.domain_ttl_seconds
        else:
            redis_key = target_suppression_key(self.job_id, self.match_id)
            ttl = self.ttl_seconds
        try:
            self._redis.sadd(redis_key, key)
            self._redis.expire(redis_key, ttl)
        except Exception:  # noqa: BLE001 - best effort, never fail a fetch on it
            logger.warning(
                "attempt_budget: could not record %s suppression of %s (%s)",
                scope.value,
                key,
                redis_key,
                exc_info=True,
            )

    def suppress_on_outcome(self, method: Any, outcome: ScrapeErrorCode | None) -> bool:
        """Suppress ``method`` for this target iff ``outcome`` justifies it.

        Returns whether a suppression was recorded, so a caller can log
        the decision. Only :data:`SUPPRESSING_OUTCOMES` qualify: a
        ``PRICE_NOT_FOUND`` is a verdict about the *listing* and a
        ``HTTP_429`` is a verdict about the *host*, and neither says this
        method cannot read this page.
        """
        if outcome not in SUPPRESSING_OUTCOMES:
            return False
        self.suppress(method, scope=SuppressionScope.TARGET)
        return True

    def is_suppressed(self, method: Any) -> bool:
        """Whether ``method`` is off-limits for this target right now.

        A TARGET-scope suppression is absolute. A DOMAIN-scope one is a
        standing rule, so a deterministically-sampled
        ``recovery_probe_fraction`` of targets report ``False`` anyway
        and probe it — see :meth:`is_recovery_probe`.
        """
        key = method_key(method)
        try:
            if self._redis.sismember(
                target_suppression_key(self.job_id, self.match_id), key
            ):
                return True
        except Exception:  # noqa: BLE001 - fail open, see module docstring
            logger.warning(
                "attempt_budget: suppression lookup failed for job=%s match=%s; "
                "treating %s as usable",
                self.job_id,
                self.match_id,
                key,
                exc_info=True,
            )
            return False

        if not self.domain:
            return False
        try:
            domain_suppressed = bool(
                self._redis.sismember(domain_suppression_key(self.domain), key)
            )
        except Exception:  # noqa: BLE001 - fail open, see module docstring
            logger.warning(
                "attempt_budget: domain suppression lookup failed for domain=%s; "
                "treating %s as usable",
                self.domain,
                key,
                exc_info=True,
            )
            return False
        if not domain_suppressed:
            return False
        return not self.is_recovery_probe(method)

    def is_recovery_probe(self, method: Any) -> bool:
        """Whether THIS target is the sampled probe for ``method``.

        Deterministic in ``(match_id, method)`` — never random — so the
        same target gives the same answer for every call in a refresh and
        two processes racing the same target cannot disagree and
        double-spend. Uniform over match ids, so the realized rate
        converges on ``recovery_probe_fraction``.
        """
        fraction = self.recovery_probe_fraction
        if fraction <= 0.0:
            return False
        if fraction >= 1.0:
            return True
        digest = hashlib.blake2b(
            f"{self.match_id}:{method_key(method)}".encode("utf-8"), digest_size=8
        ).digest()
        return (int.from_bytes(digest, "big") / float(1 << 64)) < fraction

    # -- the budget itself ------------------------------------------------

    def spent(self) -> int:
        """Physical attempts charged so far (0 when unknown)."""
        try:
            raw = self._redis.get(attempt_budget_key(self.job_id, self.match_id))
        except Exception:  # noqa: BLE001 - unknown, not zero-with-confidence
            return 0
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # -- the rationed fallback path (EPA C4) -------------------------------

    def fallback_attempts_used(self) -> int:
        """Uses of the playbook's ``fallback_path`` by this target so far.

        Read by the ladder
        (``app_shared.strategy.methods.resolve_next_physical_attempt``)
        only when the domain's playbook actually sets
        ``fallback_cap_per_refresh``; an uncapped domain never asks.

        Redis failure reports ``0`` -- fail OPEN, for the same reason the
        rest of this module does (module docstring): an invented count
        would refuse the expensive rung for every target at once on a
        cache blip, and the physical-attempt counter plus the deadline
        still bound the spend.
        """
        try:
            raw = self._redis.get(fallback_attempts_key(self.job_id, self.match_id))
        except Exception:  # noqa: BLE001 - fail open, see module docstring
            logger.warning(
                "attempt_budget: fallback counter unreadable for job=%s match=%s; "
                "treating as 0",
                self.job_id,
                self.match_id,
                exc_info=True,
            )
            return 0
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def charge_fallback_attempt(self) -> int:
        """Record one use of the fallback path; returns the new count.

        Called by the ladder AFTER :meth:`try_consume` has allowed the
        attempt, so a deadline/exhaustion refusal never burns a fallback
        slot the target did not get to use. Returns ``0`` when Redis is
        unavailable (nothing was recorded, and nothing is refused on it).
        """
        key = fallback_attempts_key(self.job_id, self.match_id)
        try:
            count = int(self._redis.incr(key))
            if count == 1:
                self._redis.expire(key, self.ttl_seconds)
            return count
        except Exception:  # noqa: BLE001 - fail open, see module docstring
            logger.warning(
                "attempt_budget: could not charge a fallback attempt (%s)",
                key,
                exc_info=True,
            )
            return 0

    def try_consume(self, method: Any, *, now: datetime | None = None) -> Verdict:
        """Charge ONE physical attempt with ``method``, or refuse it.

        Order matters and is deliberate:

        1. **Deadline** first — a target past its deadline must be
           finalized without a fetch, and must not have its counter
           advanced (the counter is evidence about attempts actually
           made).
        2. **Suppression** next — a suppressed method costs nothing, so
           it must not be charged; the caller moves to the next rung.
        3. **Counter** last, and only for an attempt that is really about
           to happen.

        A refusal is idempotent: calling again returns the same verdict.
        """
        if self.deadline_exceeded(now=now):
            return Verdict.DEADLINE_EXCEEDED
        if self.is_suppressed(method):
            return Verdict.SUPPRESSED
        if self.max_physical <= 0:
            return Verdict.BUDGET_EXHAUSTED

        key = attempt_budget_key(self.job_id, self.match_id)
        try:
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, self.ttl_seconds)
        except Exception:  # noqa: BLE001 - fail OPEN, see module docstring
            logger.warning(
                "attempt_budget: redis unavailable for %s; allowing the attempt "
                "(the target deadline still bounds it)",
                key,
                exc_info=True,
            )
            return Verdict.ALLOWED

        if int(count) > self.max_physical:
            logger.info(
                "attempt_budget: %s exhausted at attempt %s (max %d) -- refusing %s",
                key,
                count,
                self.max_physical,
                method_key(method),
            )
            return Verdict.BUDGET_EXHAUSTED
        return Verdict.ALLOWED
