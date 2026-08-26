"""EPA C2 (READY-004/READY-006): the minimum domain-state model consumed by
C3's authorization rule table.

Two areas, both offline (no database — the cache/rule-table logic is pure
Python plus a stub ``Session``):

1. :func:`authorization_rules_for_state` — the full deny-by-state matrix,
   parametrized over every :class:`DomainState` member.
2. :func:`get_domain_state` — cache-TTL behavior (a repeat call inside the
   TTL window must not re-query; a call after the TTL elapses must) and
   the unknown-domain default (no ``domain_playbooks`` row -> ``UNKNOWN``).

A stub session stands in for SQLAlchemy: ``get_domain_state`` never
inspects the ``Session`` type, only calls ``.execute(stmt).first()``, so
the stub only needs to satisfy that shape and count invocations.
"""

from __future__ import annotations

import pytest

from app_shared.domains.state_lookup import (
    DEFAULT_CACHE_SECONDS,
    DomainAuthorizationRules,
    authorization_rules_for_state,
    get_domain_state,
    reset_domain_state_cache,
)
from app_shared.models.domain_playbooks import DomainState


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts and ends with an empty per-process cache."""
    reset_domain_state_cache()
    yield
    reset_domain_state_cache()


class _FakeClock:
    """Deterministic, manually-advanced stand-in for ``time.monotonic``."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _StubSession:
    """Returns ``row`` (or ``None``, for "no matching domain_playbooks
    row") from every ``.execute(...).first()`` call, and counts calls so
    tests can assert whether the cache actually prevented a re-query."""

    def __init__(self, row=None):
        self.row = row
        self.call_count = 0

    def execute(self, stmt):
        self.call_count += 1
        return _FakeResult(self.row)


# --------------------------------------------------------------------------
# 1. Deny-by-state matrix
# --------------------------------------------------------------------------

#: state -> (paid_allowed, broad_crawl_allowed, expensive_escalation_allowed)
_EXPECTED_MATRIX: dict[DomainState, tuple[bool, bool, bool]] = {
    DomainState.QUARANTINED: (False, False, False),
    DomainState.UNSUPPORTED: (False, False, False),
    DomainState.UNKNOWN: (True, False, False),
    DomainState.DEGRADED: (True, True, False),
    DomainState.DIRECT_CANARY: (True, True, True),
    DomainState.PROFILE_CANARY: (True, True, True),
    DomainState.ACTIVE: (True, True, True),
}


def test_matrix_covers_every_domain_state():
    """The expected-matrix fixture itself must not silently drop a member
    if the enum grows — this is what would make the parametrized test
    below pass vacuously."""
    assert set(_EXPECTED_MATRIX) == set(DomainState)


@pytest.mark.parametrize("state", list(DomainState), ids=lambda s: s.value)
def test_authorization_rules_for_state_matches_matrix(state):
    expected_paid, expected_broad, expected_escalation = _EXPECTED_MATRIX[state]
    rules = authorization_rules_for_state(state)
    assert isinstance(rules, DomainAuthorizationRules)
    assert rules.paid_allowed is expected_paid
    assert rules.broad_crawl_allowed is expected_broad
    assert rules.expensive_escalation_allowed is expected_escalation


@pytest.mark.parametrize("state", [DomainState.QUARANTINED, DomainState.UNSUPPORTED])
def test_quarantined_and_unsupported_deny_all_paid(state):
    rules = authorization_rules_for_state(state)
    assert rules.paid_allowed is False
    assert rules.broad_crawl_allowed is False
    assert rules.expensive_escalation_allowed is False


def test_unknown_denies_broad_crawl_but_not_all_paid():
    rules = authorization_rules_for_state(DomainState.UNKNOWN)
    assert rules.broad_crawl_allowed is False
    # Only a tiny direct canary is permitted -- not a blanket paid ban
    # (that's QUARANTINED/UNSUPPORTED's rule, stated separately).
    assert rules.paid_allowed is True


def test_degraded_denies_expensive_escalation_only():
    rules = authorization_rules_for_state(DomainState.DEGRADED)
    assert rules.expensive_escalation_allowed is False
    assert rules.paid_allowed is True
    assert rules.broad_crawl_allowed is True


def test_active_is_fully_permitted():
    rules = authorization_rules_for_state(DomainState.ACTIVE)
    assert rules == DomainAuthorizationRules(
        paid_allowed=True, broad_crawl_allowed=True, expensive_escalation_allowed=True
    )


def test_rules_never_permit_a_narrower_gate_while_denying_a_wider_one():
    """Invariant: expensive_escalation_allowed implies broad_crawl_allowed
    implies paid_allowed, for every state -- no matrix entry may violate
    the nesting the rule table's docstring promises."""
    for state in DomainState:
        rules = authorization_rules_for_state(state)
        if rules.expensive_escalation_allowed:
            assert rules.broad_crawl_allowed is True
        if rules.broad_crawl_allowed:
            assert rules.paid_allowed is True


# --------------------------------------------------------------------------
# 2. get_domain_state: cache TTL + unknown-domain default
# --------------------------------------------------------------------------


def test_unknown_domain_with_no_playbook_row_defaults_unknown():
    session = _StubSession(row=None)
    state = get_domain_state(session, "never-seen-before.example")
    assert state is DomainState.UNKNOWN
    assert session.call_count == 1


def test_default_cache_window_is_60_seconds():
    assert DEFAULT_CACHE_SECONDS == 60


def test_repeat_call_within_ttl_does_not_requery():
    session = _StubSession(row=(DomainState.ACTIVE,))
    clock = _FakeClock()

    first = get_domain_state(session, "amazon.sa", cache_seconds=60, monotonic=clock)
    assert first is DomainState.ACTIVE
    assert session.call_count == 1

    # Mutate what the "database" would now return -- if the cache is
    # working, the second call inside the TTL window must NOT observe
    # this, and must NOT increment call_count.
    session.row = (DomainState.QUARANTINED,)
    clock.advance(59.0)
    second = get_domain_state(session, "amazon.sa", cache_seconds=60, monotonic=clock)

    assert second is DomainState.ACTIVE
    assert session.call_count == 1


def test_call_after_ttl_elapses_requeries_and_observes_new_state():
    session = _StubSession(row=(DomainState.ACTIVE,))
    clock = _FakeClock()

    first = get_domain_state(session, "amazon.sa", cache_seconds=60, monotonic=clock)
    assert first is DomainState.ACTIVE
    assert session.call_count == 1

    session.row = (DomainState.QUARANTINED,)
    clock.advance(60.0)  # exactly at the boundary: TTL has elapsed
    second = get_domain_state(session, "amazon.sa", cache_seconds=60, monotonic=clock)

    assert second is DomainState.QUARANTINED
    assert session.call_count == 2


def test_cache_is_keyed_per_domain():
    session = _StubSession(row=(DomainState.ACTIVE,))
    clock = _FakeClock()

    get_domain_state(session, "amazon.sa", monotonic=clock)
    assert session.call_count == 1

    # A different domain must not be served from amazon.sa's cache entry.
    session.row = (DomainState.UNKNOWN,)
    other = get_domain_state(session, "noon.com", monotonic=clock)
    assert other is DomainState.UNKNOWN
    assert session.call_count == 2


def test_custom_cache_seconds_is_honored():
    session = _StubSession(row=(DomainState.ACTIVE,))
    clock = _FakeClock()

    get_domain_state(session, "stech.ink", cache_seconds=5, monotonic=clock)
    assert session.call_count == 1

    session.row = (DomainState.DEGRADED,)
    clock.advance(5.0)
    result = get_domain_state(session, "stech.ink", cache_seconds=5, monotonic=clock)

    assert result is DomainState.DEGRADED
    assert session.call_count == 2


def test_reset_domain_state_cache_clears_state_between_calls():
    session = _StubSession(row=(DomainState.ACTIVE,))
    clock = _FakeClock()

    get_domain_state(session, "amazon.sa", monotonic=clock)
    assert session.call_count == 1

    reset_domain_state_cache()
    session.row = (DomainState.DEGRADED,)
    result = get_domain_state(session, "amazon.sa", monotonic=clock)

    assert result is DomainState.DEGRADED
    assert session.call_count == 2
