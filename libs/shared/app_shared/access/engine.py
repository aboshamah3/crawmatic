"""Pure attempt-decision engine (`contracts/access-engine.md`, SPEC-10 US2, FR-008/FR-009).

Stdlib only -- no SQLAlchemy/Redis/FastAPI/Scrapy imports (grep-enforced
by the caller's verification step). Turns a resolved `AccessPolicy`'s
fields + attempt history into the next transport decision
(:func:`next_attempt`) and, for a proxied plan, which provider/country/
session token to use (:func:`assign_proxy`). Exhaustively unit-testable
so the SC-001 guarantee ("a scrape follows the configured sequence in
100% of runs") lives here, not scattered across the spider.

## Judgment calls made explicit (the contract leaves these underspecified)

1. **`max_retries == 0`** forces exactly one attempt, full stop --
   `attempt_number == 2` is always `STOP`, even when
   `allow_browser_fallback=True`. This is the literal reading of the
   contract's Edge Case ("`max_retries == 0` -> ... `attempt_number == 2`
   returns `STOP`") rather than a conditional terminal-fallback step --
   an operator who explicitly configures zero retries gets exactly one
   shot, no browser escalation.
2. **Terminal fallback** (the step right after the configured retries are
   exhausted, i.e. `attempt_number == max_retries + 2`, only reachable
   when `max_retries >= 1` per (1) above) is `PLAYWRIGHT_PROXY` (intent
   only, SPEC-14 executes it) iff `allow_browser_fallback`, else `STOP`
   -- for every strategy except `DIRECT_ONLY`, which never proxies and
   therefore never escalates to a browser (`STOP` unconditionally,
   SC-001).
3. **`BROWSER_FALLBACK`** strategy: attempt 1 is always `DIRECT_HTTP`
   (no proxy) and every retry is always `PROXY_HTTP` (proxy) --
   `use_proxy_on_first_attempt`/`use_proxy_on_retry` are NOT consulted
   for this strategy (its whole purpose, encoded by strategy choice
   alone, is "try direct, then proxy, then browser"); its terminal step
   is always `PLAYWRIGHT_PROXY` regardless of `allow_browser_fallback`
   (choosing this named strategy already signals browser-fallback
   intent). `DIRECT_THEN_PROXY` is the strategy that *does* honor those
   two flags.
4. **`proxy_budget_exhausted`** degrades the *current* step only: if the
   step that would otherwise run needs a proxy and none is affordable,
   `DIRECT_THEN_PROXY`/`BROWSER_FALLBACK` fall back to the plain-HTTP
   equivalent of that step (`DIRECT_HTTP`/`DIRECT_HTTP_RETRY`, no
   proxy); `PROXY_FIRST`/`RESIDENTIAL_ONLY` have no non-proxy step to
   fall back to and `STOP`; the terminal browser-fallback step also
   needs a proxy budget, so it always `STOP`s when exhausted.
5. **`assign_proxy`'s `rotate_per_request` vs `sticky_session`** only
   affects `sticky_key` (a session-reuse token passed to the upstream
   proxy, e.g. a residential provider's "sticky session id" query
   param) -- NOT which `provider_id` is chosen. Provider selection is
   the policy's explicit `policy_provider_id` when eligible, else a
   deterministic (sorted-by-id) pick from the eligible candidate set.
   `sticky_session` -> a session_seed-derived key stable across
   `attempt_number`; `rotate_per_request` -> a key that changes with
   `attempt_number`; neither -> `None`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping

from app_shared.enums import AccessMethod, AccessStrategy, ProxyProviderStatus, ProxyType


class _Stop:
    """Singleton falsy sentinel: no further attempt (mirrors `_NoneResolved`)."""

    _instance: "_Stop | None" = None

    def __new__(cls) -> "_Stop":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "STOP"

    def __bool__(self) -> bool:
        return False


#: No further attempt -- caller finalizes the outcome (terminal failure).
STOP = _Stop()


@dataclass(frozen=True)
class AttemptPlan:
    """The next transport decision for a fetch attempt."""

    access_method: AccessMethod
    use_proxy: bool


NextAttemptResult = AttemptPlan | _Stop


def _proxy_implied(method: AccessMethod) -> bool:
    return method in (AccessMethod.PROXY_HTTP, AccessMethod.PLAYWRIGHT_PROXY)


def _normal_plan(
    strategy: AccessStrategy,
    attempt_number: int,
    use_proxy_on_first_attempt: bool,
    use_proxy_on_retry: bool,
    preferred_method: AccessMethod | None,
) -> AttemptPlan:
    """The plan for `attempt_number` within `1..max_retries+1` (non-terminal)."""
    if attempt_number == 1 and preferred_method is not None:
        # Learned-domain start (SPEC-12 forward-compat) -- the first
        # attempt starts from the learned method; the remaining chain
        # (retries) follows the strategy's normal shape unaffected by
        # this override (this function has no memory beyond
        # `attempt_number`, so "remaining chain follows" simply means
        # subsequent calls with attempt_number > 1 ignore
        # `preferred_method`).
        return AttemptPlan(preferred_method, use_proxy=_proxy_implied(preferred_method))

    if strategy == AccessStrategy.DIRECT_ONLY:
        method = AccessMethod.DIRECT_HTTP if attempt_number == 1 else AccessMethod.DIRECT_HTTP_RETRY
        return AttemptPlan(method, use_proxy=False)

    if strategy == AccessStrategy.DIRECT_THEN_PROXY:
        if attempt_number == 1:
            return AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=use_proxy_on_first_attempt)
        if use_proxy_on_retry:
            return AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)
        return AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)

    if strategy in (AccessStrategy.PROXY_FIRST, AccessStrategy.RESIDENTIAL_ONLY):
        return AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)

    if strategy == AccessStrategy.BROWSER_FALLBACK:
        # See module docstring judgment call (3): flags not consulted here.
        if attempt_number == 1:
            return AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=False)
        return AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)

    raise ValueError(f"unknown AccessStrategy: {strategy!r}")


def _terminal_plan(strategy: AccessStrategy, allow_browser_fallback: bool) -> NextAttemptResult:
    """The plan for `attempt_number == max_retries + 2` (only reached when max_retries >= 1)."""
    if strategy == AccessStrategy.DIRECT_ONLY:
        return STOP  # never a proxy, never a browser (SC-001).
    if strategy == AccessStrategy.BROWSER_FALLBACK:
        return AttemptPlan(AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True)
    if allow_browser_fallback:
        return AttemptPlan(AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True)
    return STOP


def _degrade_for_budget(
    strategy: AccessStrategy, attempt_number: int, total_direct_slots: int
) -> NextAttemptResult:
    """Reroute/stop a step that wanted a proxy but the monthly budget is exhausted."""
    if attempt_number > total_direct_slots:
        # The terminal (browser-fallback) slot also needs a proxy budget.
        return STOP
    if strategy in (AccessStrategy.PROXY_FIRST, AccessStrategy.RESIDENTIAL_ONLY):
        # No non-proxy step exists in these strategies.
        return STOP
    method = AccessMethod.DIRECT_HTTP if attempt_number == 1 else AccessMethod.DIRECT_HTTP_RETRY
    return AttemptPlan(method, use_proxy=False)


def next_attempt(
    strategy: AccessStrategy,
    *,
    attempt_number: int,
    max_retries: int,
    use_proxy_on_first_attempt: bool,
    use_proxy_on_retry: bool,
    allow_browser_fallback: bool,
    preferred_method: AccessMethod | None = None,
    proxy_budget_exhausted: bool = False,
) -> NextAttemptResult:
    """Decide the next `AttemptPlan` for `attempt_number` (1-based), or `STOP`.

    See the module docstring for the judgment calls this pure function
    encodes on top of `contracts/access-engine.md`'s behavior matrix.
    """
    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")

    total_direct_slots = max_retries + 1  # 1 initial attempt + max_retries retries

    if attempt_number <= total_direct_slots:
        plan: NextAttemptResult = _normal_plan(
            strategy, attempt_number, use_proxy_on_first_attempt, use_proxy_on_retry, preferred_method
        )
    elif attempt_number == total_direct_slots + 1 and max_retries >= 1:
        plan = _terminal_plan(strategy, allow_browser_fallback)
    else:
        return STOP

    if plan is STOP:
        return STOP

    assert isinstance(plan, AttemptPlan)
    if proxy_budget_exhausted and plan.use_proxy:
        return _degrade_for_budget(strategy, attempt_number, total_direct_slots)

    return plan


@dataclass(frozen=True)
class ProxyAssignment:
    """A concrete provider+country+session choice for a proxied attempt."""

    provider_id: uuid.UUID
    country: str | None
    sticky_key: str | None  # set when sticky_session (session reuse token)


#: `{provider_id: (status, type, country)}` -- own+global visible providers.
VisibleProviders = Mapping[uuid.UUID, tuple[ProxyProviderStatus, ProxyType, str | None]]


def assign_proxy(
    *,
    strategy: AccessStrategy,
    policy_provider_id: uuid.UUID | None,
    policy_country: str | None,
    domain_rule_country: str | None,
    visible_providers: VisibleProviders,
    attempt_number: int,
    rotate_per_request: bool,
    sticky_session: bool,
    session_seed: str,
) -> ProxyAssignment | None:
    """Pick provider+country per policy/domain-rule, honoring rotation vs sticky.

    `visible_providers` is the caller-loaded own+global visibility map
    (bounded, never a per-call query -- this function performs no I/O).
    For `RESIDENTIAL_ONLY`, candidates are restricted to
    `ProxyType.RESIDENTIAL` providers. Returns `None` when no eligible
    provider is visible (DISABLED/absent, or none residential for
    `RESIDENTIAL_ONLY`) -- the caller degrades (falls back per strategy
    or fails `PROXY_FAILED`).

    Country precedence: `domain_rule_country` (most specific) ->
    `policy_country` -> the chosen provider's own `country_code` ->
    `None`.
    """
    candidates = {
        provider_id: entry
        for provider_id, entry in visible_providers.items()
        if entry[0] == ProxyProviderStatus.ACTIVE
    }
    if strategy == AccessStrategy.RESIDENTIAL_ONLY:
        candidates = {
            provider_id: entry for provider_id, entry in candidates.items() if entry[1] == ProxyType.RESIDENTIAL
        }
    if not candidates:
        return None

    if policy_provider_id is not None:
        if policy_provider_id not in candidates:
            return None
        chosen_id = policy_provider_id
    else:
        # Deterministic pick among eligible candidates (sorted by id) --
        # no policy-declared provider to prefer.
        chosen_id = sorted(candidates, key=str)[0]

    _, _, provider_country = candidates[chosen_id]
    country = domain_rule_country or policy_country or provider_country

    if rotate_per_request:
        sticky_key: str | None = f"{session_seed}:{attempt_number}"
    elif sticky_session:
        sticky_key = f"{session_seed}"
    else:
        sticky_key = None

    return ProxyAssignment(provider_id=chosen_id, country=country, sticky_key=sticky_key)
