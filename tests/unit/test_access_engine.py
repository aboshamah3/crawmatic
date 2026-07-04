"""`app_shared/access/engine.py` unit tests (SPEC-10 US2 T028,
`contracts/access-engine.md` Acceptance, FR-008/FR-009, SC-001).

Pure in-memory exercises of the attempt-decision engine -- no DB, no
Redis, no reactor.
"""

from __future__ import annotations

import itertools
import uuid

from app_shared.access.engine import (
    STOP,
    AttemptPlan,
    ProxyAssignment,
    assign_proxy,
    next_attempt,
)
from app_shared.enums import AccessMethod, AccessStrategy, ProxyProviderStatus, ProxyType

ALL_STRATEGIES = list(AccessStrategy)
FLAG_COMBOS = list(itertools.product([False, True], repeat=3))  # (first, retry, browser)


# --- DIRECT_ONLY: never a proxy, for ANY input (SC-001) ---------------------


def test_direct_only_never_proxies_across_every_combo() -> None:
    for max_retries in range(0, 4):
        for attempt_number in range(1, max_retries + 4):
            for use_first, use_retry, allow_browser in FLAG_COMBOS:
                for proxy_budget_exhausted in (False, True):
                    plan = next_attempt(
                        AccessStrategy.DIRECT_ONLY,
                        attempt_number=attempt_number,
                        max_retries=max_retries,
                        use_proxy_on_first_attempt=use_first,
                        use_proxy_on_retry=use_retry,
                        allow_browser_fallback=allow_browser,
                        proxy_budget_exhausted=proxy_budget_exhausted,
                    )
                    if plan is not STOP:
                        assert isinstance(plan, AttemptPlan)
                        assert plan.use_proxy is False


def test_direct_only_attempt_shape() -> None:
    assert next_attempt(
        AccessStrategy.DIRECT_ONLY,
        attempt_number=1,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    ) == AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=False)

    assert next_attempt(
        AccessStrategy.DIRECT_ONLY,
        attempt_number=2,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    ) == AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)

    assert (
        next_attempt(
            AccessStrategy.DIRECT_ONLY,
            attempt_number=3,
            max_retries=2,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=False,
        )
        == AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)
    )

    # Terminal: DIRECT_ONLY never escalates to a browser, even when allowed.
    assert (
        next_attempt(
            AccessStrategy.DIRECT_ONLY,
            attempt_number=4,
            max_retries=2,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=True,
        )
        is STOP
    )


# --- DIRECT_THEN_PROXY: direct first, PROXY_HTTP on retry -------------------


def test_direct_then_proxy_attempt1_honors_use_proxy_on_first_attempt() -> None:
    for use_first in (False, True):
        plan = next_attempt(
            AccessStrategy.DIRECT_THEN_PROXY,
            attempt_number=1,
            max_retries=2,
            use_proxy_on_first_attempt=use_first,
            use_proxy_on_retry=True,
            allow_browser_fallback=False,
        )
        assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=use_first)


def test_direct_then_proxy_retries_via_proxy_http_when_flag_set() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=2,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    assert plan == AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)


def test_direct_then_proxy_retries_direct_when_retry_flag_unset() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=2,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)


def test_direct_then_proxy_terminal_is_playwright_when_allowed() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=4,  # max_retries + 2
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=True,
    )
    assert plan == AttemptPlan(AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True)


def test_direct_then_proxy_terminal_is_stop_when_not_allowed() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=4,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    assert plan is STOP


def test_direct_then_proxy_beyond_terminal_is_always_stop() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=5,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=True,
    )
    assert plan is STOP


# --- max_retries == 0 Edge Case: exactly one attempt, then STOP -------------


def test_max_retries_zero_yields_exactly_one_attempt_then_stop() -> None:
    for strategy in ALL_STRATEGIES:
        for allow_browser in (False, True):
            plan = next_attempt(
                strategy,
                attempt_number=1,
                max_retries=0,
                use_proxy_on_first_attempt=True,
                use_proxy_on_retry=True,
                allow_browser_fallback=allow_browser,
            )
            assert plan is not STOP, f"{strategy} attempt 1 should not be STOP"

            stopped = next_attempt(
                strategy,
                attempt_number=2,
                max_retries=0,
                use_proxy_on_first_attempt=True,
                use_proxy_on_retry=True,
                allow_browser_fallback=allow_browser,
            )
            assert stopped is STOP, (
                f"{strategy} with max_retries=0 must STOP at attempt 2 "
                f"regardless of allow_browser_fallback={allow_browser}"
            )


# --- PROXY_FIRST / RESIDENTIAL_ONLY: proxied from attempt 1 -----------------


def test_proxy_first_and_residential_only_proxy_from_attempt_one() -> None:
    for strategy in (AccessStrategy.PROXY_FIRST, AccessStrategy.RESIDENTIAL_ONLY):
        for attempt_number in (1, 2, 3):
            plan = next_attempt(
                strategy,
                attempt_number=attempt_number,
                max_retries=2,
                use_proxy_on_first_attempt=False,
                use_proxy_on_retry=False,
                allow_browser_fallback=False,
            )
            assert plan == AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)


def test_proxy_first_terminal_fallback_matrix() -> None:
    allowed = next_attempt(
        AccessStrategy.PROXY_FIRST,
        attempt_number=4,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=True,
    )
    assert allowed == AttemptPlan(AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True)

    not_allowed = next_attempt(
        AccessStrategy.RESIDENTIAL_ONLY,
        attempt_number=4,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    assert not_allowed is STOP


# --- BROWSER_FALLBACK: direct once, proxy retries, browser terminal --------


def test_browser_fallback_shape_ignores_use_proxy_flags() -> None:
    for use_first, use_retry in itertools.product([False, True], repeat=2):
        attempt1 = next_attempt(
            AccessStrategy.BROWSER_FALLBACK,
            attempt_number=1,
            max_retries=2,
            use_proxy_on_first_attempt=use_first,
            use_proxy_on_retry=use_retry,
            allow_browser_fallback=False,
        )
        assert attempt1 == AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=False)

        retry = next_attempt(
            AccessStrategy.BROWSER_FALLBACK,
            attempt_number=2,
            max_retries=2,
            use_proxy_on_first_attempt=use_first,
            use_proxy_on_retry=use_retry,
            allow_browser_fallback=False,
        )
        assert retry == AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)


def test_browser_fallback_terminal_is_always_playwright_regardless_of_flag() -> None:
    for allow_browser in (False, True):
        terminal = next_attempt(
            AccessStrategy.BROWSER_FALLBACK,
            attempt_number=4,
            max_retries=2,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=allow_browser,
        )
        assert terminal == AttemptPlan(AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True)


# --- proxy_budget_exhausted reroutes/stops per strategy ---------------------


def test_budget_exhausted_direct_then_proxy_attempt1_falls_back_to_direct() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=1,
        max_retries=2,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
        proxy_budget_exhausted=True,
    )
    assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=False)


def test_budget_exhausted_direct_then_proxy_retry_falls_back_to_direct_retry() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=2,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
        proxy_budget_exhausted=True,
    )
    assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)


def test_budget_exhausted_proxy_first_stops_no_non_proxy_alternative() -> None:
    for attempt_number in (1, 2):
        plan = next_attempt(
            AccessStrategy.PROXY_FIRST,
            attempt_number=attempt_number,
            max_retries=2,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=False,
            proxy_budget_exhausted=True,
        )
        assert plan is STOP


def test_budget_exhausted_residential_only_stops() -> None:
    plan = next_attempt(
        AccessStrategy.RESIDENTIAL_ONLY,
        attempt_number=1,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
        proxy_budget_exhausted=True,
    )
    assert plan is STOP


def test_budget_exhausted_terminal_browser_step_always_stops() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=4,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=True,
        proxy_budget_exhausted=True,
    )
    assert plan is STOP


def test_budget_exhausted_does_not_affect_a_non_proxy_step() -> None:
    # attempt 1 of DIRECT_THEN_PROXY with use_proxy_on_first_attempt=False
    # was never going to proxy -- budget exhaustion is a no-op here.
    plan = next_attempt(
        AccessStrategy.DIRECT_THEN_PROXY,
        attempt_number=1,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
        proxy_budget_exhausted=True,
    )
    assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP, use_proxy=False)


# --- preferred_method: learned-domain start ---------------------------------


def test_preferred_method_starts_attempt_one_only() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_ONLY,  # even a strategy that would never proxy...
        attempt_number=1,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
        preferred_method=AccessMethod.PROXY_HTTP,  # ...honors a learned proxy start.
    )
    assert plan == AttemptPlan(AccessMethod.PROXY_HTTP, use_proxy=True)


def test_preferred_method_does_not_affect_later_attempts() -> None:
    plan = next_attempt(
        AccessStrategy.DIRECT_ONLY,
        attempt_number=2,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
        preferred_method=AccessMethod.PROXY_HTTP,
    )
    assert plan == AttemptPlan(AccessMethod.DIRECT_HTTP_RETRY, use_proxy=False)


# --- attempt_number validation -----------------------------------------------


def test_attempt_number_below_one_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        next_attempt(
            AccessStrategy.DIRECT_ONLY,
            attempt_number=0,
            max_retries=2,
            use_proxy_on_first_attempt=False,
            use_proxy_on_retry=False,
            allow_browser_fallback=False,
        )


# --- assign_proxy ------------------------------------------------------------


def _provider(status: ProxyProviderStatus, ptype: ProxyType, country: str | None = None):
    return (status, ptype, country)


def test_assign_proxy_uses_policy_provider_id_when_eligible() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER, "US")}

    assignment = assign_proxy(
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        policy_provider_id=provider_id,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )

    assert assignment == ProxyAssignment(provider_id=provider_id, country="US", sticky_key=None)


def test_assign_proxy_country_precedence_domain_rule_beats_policy_beats_provider() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER, "US")}

    assignment = assign_proxy(
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        policy_provider_id=provider_id,
        policy_country="CA",
        domain_rule_country="DE",
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is not None
    assert assignment.country == "DE"

    assignment2 = assign_proxy(
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        policy_provider_id=provider_id,
        policy_country="CA",
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment2 is not None
    assert assignment2.country == "CA"


def test_assign_proxy_returns_none_for_disabled_provider() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.DISABLED, ProxyType.DATACENTER)}

    assignment = assign_proxy(
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        policy_provider_id=provider_id,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is None


def test_assign_proxy_returns_none_for_missing_provider() -> None:
    provider_id = uuid.uuid4()
    assignment = assign_proxy(
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        policy_provider_id=provider_id,
        policy_country=None,
        domain_rule_country=None,
        visible_providers={},
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is None


def test_assign_proxy_no_policy_provider_picks_deterministically_from_candidates() -> None:
    provider_a = uuid.uuid4()
    provider_b = uuid.uuid4()
    visible = {
        provider_a: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER),
        provider_b: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER),
    }
    expected = sorted([provider_a, provider_b], key=str)[0]

    assignment = assign_proxy(
        strategy=AccessStrategy.PROXY_FIRST,
        policy_provider_id=None,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is not None
    assert assignment.provider_id == expected


def test_assign_proxy_residential_only_restricts_candidates() -> None:
    datacenter_id = uuid.uuid4()
    residential_id = uuid.uuid4()
    visible = {
        datacenter_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER),
        residential_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.RESIDENTIAL),
    }

    assignment = assign_proxy(
        strategy=AccessStrategy.RESIDENTIAL_ONLY,
        policy_provider_id=None,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is not None
    assert assignment.provider_id == residential_id


def test_assign_proxy_residential_only_returns_none_when_only_datacenter_visible() -> None:
    datacenter_id = uuid.uuid4()
    visible = {datacenter_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER)}

    assignment = assign_proxy(
        strategy=AccessStrategy.RESIDENTIAL_ONLY,
        policy_provider_id=None,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is None


def test_assign_proxy_sticky_session_key_is_stable_across_attempts() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER)}

    keys = {
        assign_proxy(
            strategy=AccessStrategy.PROXY_FIRST,
            policy_provider_id=provider_id,
            policy_country=None,
            domain_rule_country=None,
            visible_providers=visible,
            attempt_number=attempt_number,
            rotate_per_request=False,
            sticky_session=True,
            session_seed="stable-seed",
        ).sticky_key
        for attempt_number in (1, 2, 3)
    }
    assert len(keys) == 1
    assert None not in keys


def test_assign_proxy_rotate_per_request_key_differs_across_attempts() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER)}

    keys = {
        assign_proxy(
            strategy=AccessStrategy.PROXY_FIRST,
            policy_provider_id=provider_id,
            policy_country=None,
            domain_rule_country=None,
            visible_providers=visible,
            attempt_number=attempt_number,
            rotate_per_request=True,
            sticky_session=False,
            session_seed="rotate-seed",
        ).sticky_key
        for attempt_number in (1, 2, 3)
    }
    assert len(keys) == 3


def test_assign_proxy_neither_sticky_nor_rotate_has_no_sticky_key() -> None:
    provider_id = uuid.uuid4()
    visible = {provider_id: _provider(ProxyProviderStatus.ACTIVE, ProxyType.DATACENTER)}

    assignment = assign_proxy(
        strategy=AccessStrategy.PROXY_FIRST,
        policy_provider_id=provider_id,
        policy_country=None,
        domain_rule_country=None,
        visible_providers=visible,
        attempt_number=1,
        rotate_per_request=False,
        sticky_session=False,
        session_seed="seed",
    )
    assert assignment is not None
    assert assignment.sticky_key is None
