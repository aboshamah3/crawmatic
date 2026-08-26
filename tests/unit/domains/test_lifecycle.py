"""EPA W4.1: the full domain certification lifecycle (report §6, builds
on C2's minimum).

Offline (no database) — same style as
``tests/unit/domains/test_state_lookup.py``: :func:`transition` only
needs ``session.execute(stmt).scalar_one_or_none()`` and
``session.add(obj)``, so a tiny stub session stands in for SQLAlchemy.
The real DB round-trip (migration up/down/up, append-only trigger,
FK to ``domain_playbooks``) is proven separately against the A4 scratch
restore per the task contract's Step 5, not here.

Covers the task's Step 1 failing tests:
1. ``PROFILE_CANARY -> ACTIVE`` requires a recorded human approval.
2. ``UNSUPPORTED`` produces a product-visible outcome and zero retries.
3. Recertification restores from ``DEGRADED``.
4. Every transition writes an audit row with evidence + approver.
5. A transition FROM a seeded state (no prior audit rows) writes the
   first audit row for that domain.

Plus the W4 gate-review follow-ups (2026-08-26), sections 7-8:
6. The approval gate is DERIVED from C3's rule table
   (``authorization_rules_for_state``), closing the one-hop approval
   bypass ``UNKNOWN -> DIRECT_CANARY`` used to allow -- while leaving
   every capability-neutral/reducing edge (automatic degrade, operator
   quarantine, the lateral canary move) approver-free.
7. A transition into a canary state stamps ``last_canary_at``, and
   ``profile_owner`` is recordable without ever being cleared by
   omission.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app_shared.domains.lifecycle import (
    ALLOWED_TRANSITIONS,
    CAPABILITY_GATES,
    UNSUPPORTED_ERROR_CODE,
    UNSUPPORTED_TARGET_STATUS,
    ApprovalRequiredError,
    InvalidTransitionError,
    approval_required,
    capabilities_granted,
    transition,
    unsupported_target_outcome,
)
from app_shared.domains.state_lookup import (
    DomainAuthorizationRules,
    authorization_rules_for_state,
)
from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.models.domain_playbooks import DomainLifecycleAudit, DomainPlaybook, DomainState


# --------------------------------------------------------------------------
# Stub session -- mirrors test_state_lookup.py's _StubSession, extended
# with .add() since transition() also writes an audit row.
# --------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _StubSession:
    """Returns ``playbook`` from every ``.execute(...).scalar_one_or_none()``
    call (ignoring the actual ``stmt``, same simplification
    ``test_state_lookup.py``'s stub makes) and records every ``.add()``ed
    object so tests can inspect the audit row(s) a call produced."""

    def __init__(self, playbook: DomainPlaybook | None):
        self.playbook = playbook
        self.added: list = []

    def execute(self, stmt):
        return _FakeScalarResult(self.playbook)

    def add(self, instance) -> None:
        self.added.append(instance)


def _playbook(domain: str, state: DomainState, profile_version: int = 1) -> DomainPlaybook:
    """A bare, unpersisted ``DomainPlaybook`` -- enough for ``transition``,
    which only ever reads/writes ``.state``/``.profile_version`` and never
    touches columns needing a DB default (``id``, ``created_at``, ...)."""
    pb = DomainPlaybook(
        domain=domain,
        preferred_access_method="DIRECT_HTTP",
        state=state,
        profile_version=profile_version,
    )
    return pb


# --------------------------------------------------------------------------
# 1. PROFILE_CANARY -> ACTIVE requires a recorded human approval
# --------------------------------------------------------------------------


def test_profile_canary_to_active_without_approver_raises():
    session = _StubSession(_playbook("amazon.sa", DomainState.PROFILE_CANARY))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session,
            "amazon.sa",
            DomainState.ACTIVE,
            evidence={"canary": "5/5"},
            approver=None,
        )
    # Refused before any mutation or audit row.
    assert session.playbook.state is DomainState.PROFILE_CANARY
    assert session.added == []


def test_profile_canary_to_active_with_approver_succeeds():
    session = _StubSession(_playbook("amazon.sa", DomainState.PROFILE_CANARY))
    row = transition(
        session,
        "amazon.sa",
        DomainState.ACTIVE,
        evidence={"canary": "5/5"},
        approver="ops@crawmatic.com",
    )
    assert session.playbook.state is DomainState.ACTIVE
    assert row.approver == "ops@crawmatic.com"
    assert row in session.added


def test_empty_approver_string_is_treated_as_no_approver():
    """An empty string is not a recorded human approval -- same falsy
    check as ``None``."""
    session = _StubSession(_playbook("amazon.sa", DomainState.PROFILE_CANARY))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session, "amazon.sa", DomainState.ACTIVE, evidence={"canary": "5/5"}, approver=""
        )


# --------------------------------------------------------------------------
# 2. UNSUPPORTED: product-visible outcome + zero retries
# --------------------------------------------------------------------------


def test_unsupported_outcome_is_terminal_skipped_not_retryable_deferred():
    status, code = unsupported_target_outcome()
    assert status is UNSUPPORTED_TARGET_STATUS
    assert code is UNSUPPORTED_ERROR_CODE
    assert status is ScrapeTargetStatus.SKIPPED
    assert isinstance(code, ScrapeErrorCode)
    # "Zero retries" as a structural property, not a policy this test
    # merely trusts: SKIPPED is a member of app_shared.jobs.targets'
    # terminal-status set (mark_target never transitions a terminal
    # target again, and dispatch only ever re-picks PENDING/DEFERRED
    # targets), so choosing SKIPPED over DEFERRED IS the zero-retry
    # guarantee. Assert it directly against the real terminal set rather
    # than re-declaring one here, so a future change to that set is
    # caught by this test too.
    from app_shared.jobs.targets import _TERMINAL_TARGET_STATUSES

    assert status in _TERMINAL_TARGET_STATUSES
    assert status is not ScrapeTargetStatus.DEFERRED


def test_unsupported_outcome_is_a_stable_pure_singleton_pair():
    """Calling twice returns the identical contract -- no hidden state,
    no randomness, safe to call from any dispatch path repeatedly."""
    assert unsupported_target_outcome() == unsupported_target_outcome()


def test_transition_to_unsupported_does_not_require_approval():
    """UNSUPPORTED denies every capability -- a capability-REDUCING edge,
    so no approver is required even under the widened, table-derived
    gate (W4 gate review)."""
    session = _StubSession(_playbook("bad-domain.example", DomainState.UNKNOWN))
    row = transition(
        session,
        "bad-domain.example",
        DomainState.UNSUPPORTED,
        evidence={"reason": "no supported method reaches this origin"},
        approver=None,
    )
    assert session.playbook.state is DomainState.UNSUPPORTED
    assert row.approver is None


# --------------------------------------------------------------------------
# 3. Recertification restores from DEGRADED
# --------------------------------------------------------------------------


def test_degraded_to_active_recertification_requires_approval():
    session = _StubSession(_playbook("noon.com", DomainState.DEGRADED))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session, "noon.com", DomainState.ACTIVE, evidence={"recert": "21/21"}, approver=None
        )


def test_degraded_to_active_recertification_with_approval_succeeds():
    session = _StubSession(_playbook("noon.com", DomainState.DEGRADED, profile_version=3))
    row = transition(
        session,
        "noon.com",
        DomainState.ACTIVE,
        evidence={"recert": "21/21"},
        approver="ops@crawmatic.com",
    )
    assert session.playbook.state is DomainState.ACTIVE
    assert session.playbook.profile_version == 4
    assert row.to_state == DomainState.ACTIVE.value
    assert row.from_state == DomainState.DEGRADED.value
    assert row.profile_version == 4


def test_degraded_to_profile_canary_recanary_requires_approval():
    """W4 gate review (2026-08-26): re-canarying out of ``DEGRADED`` is
    NOT a capability-neutral lateral move -- it restores
    ``expensive_escalation_allowed``, the single capability ``DEGRADED``
    exists to withhold, so it is a capability-granting edge and needs a
    recorded human approver like any other. (This test previously
    asserted the opposite; the table-derived gate corrected it.)"""
    session = _StubSession(_playbook("noon.com", DomainState.DEGRADED))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session,
            "noon.com",
            DomainState.PROFILE_CANARY,
            evidence={"reason": "re-canary before recert"},
            approver=None,
        )
    assert session.playbook.state is DomainState.DEGRADED
    assert session.added == []

    row = transition(
        session,
        "noon.com",
        DomainState.PROFILE_CANARY,
        evidence={"reason": "re-canary before recert"},
        approver="ops@crawmatic.com",
    )
    assert row.approver == "ops@crawmatic.com"
    assert session.playbook.state is DomainState.PROFILE_CANARY


# --------------------------------------------------------------------------
# 4. Every transition writes an audit row with evidence + approver
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state,approver",
    [
        # Capability-granting edges carry an approver (W4 gate review):
        # UNKNOWN -> DIRECT_CANARY grants broad crawl + expensive
        # escalation, and QUARANTINED -> UNKNOWN restores paid work.
        (DomainState.UNKNOWN, DomainState.DIRECT_CANARY, "ops@crawmatic.com"),
        (DomainState.QUARANTINED, DomainState.UNKNOWN, "ops@crawmatic.com"),
        (DomainState.PROFILE_CANARY, DomainState.ACTIVE, "ops@crawmatic.com"),
        # Capability-neutral / -reducing edges do not.
        (DomainState.DIRECT_CANARY, DomainState.PROFILE_CANARY, None),
        (DomainState.ACTIVE, DomainState.DEGRADED, None),
    ],
)
def test_every_transition_writes_exactly_one_audit_row(from_state, to_state, approver):
    session = _StubSession(_playbook("example.com", from_state))
    evidence = {"note": "test evidence"}
    row = transition(session, "example.com", to_state, evidence=evidence, approver=approver)

    assert isinstance(row, DomainLifecycleAudit)
    assert row.domain == "example.com"
    assert row.from_state == from_state.value
    assert row.to_state == to_state.value
    assert row.evidence == evidence
    assert row.approver == approver
    assert session.added == [row]


def test_missing_evidence_raises_and_writes_no_audit_row():
    session = _StubSession(_playbook("example.com", DomainState.UNKNOWN))
    with pytest.raises(InvalidTransitionError):
        transition(session, "example.com", DomainState.DIRECT_CANARY, evidence={})
    assert session.added == []
    # No mutation either -- a refused transition must not have side effects.
    assert session.playbook.state is DomainState.UNKNOWN


def test_none_evidence_raises():
    session = _StubSession(_playbook("example.com", DomainState.UNKNOWN))
    with pytest.raises(InvalidTransitionError):
        transition(session, "example.com", DomainState.DIRECT_CANARY, evidence=None)


# --------------------------------------------------------------------------
# 5. Transition FROM a seeded state writes the first audit row
# --------------------------------------------------------------------------


def test_transition_from_seeded_active_state_writes_first_audit_row():
    """C2's migration seeded amazon.sa/noon.com/stech.ink to ACTIVE by a
    direct UPDATE, not through transition() -- there is no audit row
    explaining why they started ACTIVE. The first transition() call
    against a seeded domain is therefore the FIRST row this table ever
    produces for it: from_state correctly reflects the seeded value,
    with nothing before it in the (simulated) audit history."""
    seeded = _playbook("amazon.sa", DomainState.ACTIVE, profile_version=1)
    session = _StubSession(seeded)

    # Nothing written yet -- mirrors an empty domain_lifecycle_audit
    # table for this domain before any transition() call.
    assert session.added == []

    row = transition(
        session,
        "amazon.sa",
        DomainState.DEGRADED,
        evidence={"reason": "failure signal spike"},
        approver=None,
    )

    assert len(session.added) == 1
    assert session.added[0] is row
    assert row.from_state == DomainState.ACTIVE.value
    assert row.to_state == DomainState.DEGRADED.value


# --------------------------------------------------------------------------
# 6. Invalid transitions / graph coverage
# --------------------------------------------------------------------------


def test_allowed_transitions_covers_every_domain_state():
    assert set(ALLOWED_TRANSITIONS) == set(DomainState)


def test_no_state_has_itself_as_an_allowed_destination():
    for state, destinations in ALLOWED_TRANSITIONS.items():
        assert state not in destinations


def test_illegal_edge_raises_invalid_transition():
    # QUARANTINED -> ACTIVE is not in the graph (must pass through
    # UNKNOWN/DIRECT_CANARY/PROFILE_CANARY first).
    session = _StubSession(_playbook("example.com", DomainState.QUARANTINED))
    with pytest.raises(InvalidTransitionError):
        transition(
            session,
            "example.com",
            DomainState.ACTIVE,
            evidence={"x": 1},
            approver="ops@crawmatic.com",
        )
    assert session.added == []


def test_unknown_domain_with_no_playbook_row_raises_invalid_transition():
    session = _StubSession(playbook=None)
    with pytest.raises(InvalidTransitionError):
        transition(
            session, "never-seen.example", DomainState.DIRECT_CANARY, evidence={"x": 1}
        )
    assert session.added == []


def test_profile_version_increments_on_every_successful_transition():
    session = _StubSession(_playbook("example.com", DomainState.UNKNOWN, profile_version=5))
    row = transition(
        session,
        "example.com",
        DomainState.DIRECT_CANARY,
        evidence={"x": 1},
        approver="ops@crawmatic.com",
    )
    assert session.playbook.profile_version == 6
    assert row.profile_version == 6


def test_profile_version_unchanged_on_refused_transition():
    session = _StubSession(_playbook("example.com", DomainState.UNKNOWN, profile_version=5))
    with pytest.raises(InvalidTransitionError):
        transition(session, "example.com", DomainState.ACTIVE, evidence={"x": 1})
    assert session.playbook.profile_version == 5


# --------------------------------------------------------------------------
# 7. W4 gate review (2026-08-26): the approval gate is derived from C3's
#    rule table, and closes the one-hop approval bypass
# --------------------------------------------------------------------------


def test_unknown_to_direct_canary_without_approver_is_refused():
    """THE gate-review finding, pinned.

    The first W4 cut gated approval on ``to_state is ACTIVE`` alone. But
    ``authorization_rules_for_state`` grants ``DIRECT_CANARY`` exactly
    what it grants ``ACTIVE`` -- paid work, broad crawl, and expensive
    escalation all permitted -- and ``UNKNOWN -> DIRECT_CANARY`` needed
    no approver. That was a ONE-HOP APPROVAL BYPASS: an uncertified
    domain could reach full C3 authorization in a single unapproved
    move without ever entering ``ACTIVE``.

    This test previously asserted that this edge PASSED with
    ``approver=None``. It is inverted deliberately: the edge grants
    capability, so it now requires a recorded human approver.
    """
    session = _StubSession(_playbook("brand-new.example", DomainState.UNKNOWN))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session,
            "brand-new.example",
            DomainState.DIRECT_CANARY,
            evidence={"canary": "planned 5 direct fetches"},
            approver=None,
        )
    # Refused before any mutation or audit row -- no partial bypass.
    assert session.playbook.state is DomainState.UNKNOWN
    assert session.playbook.profile_version == 1
    assert session.added == []


def test_unknown_to_direct_canary_with_approver_succeeds_and_records_approval():
    session = _StubSession(_playbook("brand-new.example", DomainState.UNKNOWN))
    row = transition(
        session,
        "brand-new.example",
        DomainState.DIRECT_CANARY,
        evidence={"canary": "planned 5 direct fetches"},
        approver="ops@crawmatic.com",
    )
    assert session.playbook.state is DomainState.DIRECT_CANARY
    # The approval is durable: it lands on the append-only audit row,
    # not merely on the (mutable) playbook row.
    assert row.approver == "ops@crawmatic.com"
    assert row.from_state == DomainState.UNKNOWN.value
    assert row.to_state == DomainState.DIRECT_CANARY.value
    assert session.added == [row]


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (DomainState.ACTIVE, DomainState.DEGRADED),
        (DomainState.ACTIVE, DomainState.QUARANTINED),
        (DomainState.PROFILE_CANARY, DomainState.QUARANTINED),
        (DomainState.DIRECT_CANARY, DomainState.UNSUPPORTED),
        (DomainState.DIRECT_CANARY, DomainState.PROFILE_CANARY),
        (DomainState.DIRECT_CANARY, DomainState.UNKNOWN),
    ],
)
def test_capability_neutral_or_reducing_edges_stay_auto_allowed(from_state, to_state):
    """Widening the gate must NOT break the automatic paths: a failure
    signal degrading a domain, an operator quarantine, a certification
    giving up on a domain, and the lateral canary-ladder move all still
    run with no human in the loop (``approver=None``)."""
    session = _StubSession(_playbook("example.com", from_state))
    row = transition(session, "example.com", to_state, evidence={"x": 1}, approver=None)
    assert row.approver is None
    assert session.playbook.state is to_state


def test_approval_gate_matches_the_rule_table_for_every_legal_edge():
    """The gate is DERIVED, not restated: for every legal edge, requiring
    an approver must be equivalent to "the destination's rule-table row
    grants something the source's denies", union the ``-> ACTIVE``
    certification edge. If someone re-hardcodes the gate as a list of
    edges, this test fails the moment the rule table moves."""
    for from_state, destinations in ALLOWED_TRANSITIONS.items():
        for to_state in destinations:
            source = authorization_rules_for_state(from_state)
            destination = authorization_rules_for_state(to_state)
            expected = to_state is DomainState.ACTIVE or any(
                getattr(destination, gate) and not getattr(source, gate)
                for gate in CAPABILITY_GATES
            )
            assert approval_required(from_state, to_state) is expected, (
                f"{from_state.value} -> {to_state.value}"
            )


def test_capability_gates_are_read_off_the_rule_table_dataclass():
    """Not a hand-written list: adding a fourth gate to
    ``DomainAuthorizationRules`` extends the approval gate automatically
    instead of silently falling outside it."""
    assert CAPABILITY_GATES == tuple(DomainAuthorizationRules.__dataclass_fields__)


def test_capabilities_granted_names_exactly_what_the_edge_hands_out():
    assert capabilities_granted(DomainState.UNKNOWN, DomainState.DIRECT_CANARY) == (
        "broad_crawl_allowed",
        "expensive_escalation_allowed",
    )
    assert capabilities_granted(DomainState.DEGRADED, DomainState.ACTIVE) == (
        "expensive_escalation_allowed",
    )
    assert capabilities_granted(DomainState.QUARANTINED, DomainState.UNKNOWN) == (
        "paid_allowed",
    )
    # Reducing and neutral edges hand out nothing.
    assert capabilities_granted(DomainState.ACTIVE, DomainState.DEGRADED) == ()
    assert capabilities_granted(DomainState.ACTIVE, DomainState.QUARANTINED) == ()
    assert capabilities_granted(DomainState.PROFILE_CANARY, DomainState.ACTIVE) == ()


def test_profile_canary_to_active_still_gated_even_though_table_is_neutral():
    """``PROFILE_CANARY`` and ``ACTIVE`` are byte-identical in the rule
    table, so the table-derived half of the gate grants nothing here --
    the ``-> ACTIVE`` half is what keeps full certification a human act.
    The union is why widening the gate could not accidentally UNGATE
    the certification edge."""
    assert capabilities_granted(DomainState.PROFILE_CANARY, DomainState.ACTIVE) == ()
    assert approval_required(DomainState.PROFILE_CANARY, DomainState.ACTIVE) is True


def test_degraded_to_active_recertification_still_requires_approver():
    """Recertification stays gated under the widened rule -- by BOTH
    halves now (it grants ``expensive_escalation_allowed`` and it enters
    ``ACTIVE``)."""
    assert approval_required(DomainState.DEGRADED, DomainState.ACTIVE) is True
    session = _StubSession(_playbook("noon.com", DomainState.DEGRADED))
    with pytest.raises(ApprovalRequiredError):
        transition(
            session, "noon.com", DomainState.ACTIVE, evidence={"recert": "21/21"}, approver=None
        )
    assert session.added == []


# --------------------------------------------------------------------------
# 8. Canary edges stamp last_canary_at; profile_owner is recordable
# --------------------------------------------------------------------------

_CANARY_NOW = datetime(2026, 8, 26, 3, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (DomainState.UNKNOWN, DomainState.DIRECT_CANARY),
        (DomainState.DIRECT_CANARY, DomainState.PROFILE_CANARY),
        (DomainState.PROFILE_CANARY, DomainState.DIRECT_CANARY),
    ],
)
def test_transition_into_a_canary_state_stamps_last_canary_at(from_state, to_state):
    """The transition IS what starts the canary, so the timestamp lands
    on the same unit of work as the state that makes the canary legal --
    it records that a canary RAN, not that it passed (that column's own
    docstring)."""
    session = _StubSession(_playbook("shop.example", from_state))
    assert session.playbook.last_canary_at is None

    transition(
        session,
        "shop.example",
        to_state,
        evidence={"canary": "scheduled"},
        approver="ops@crawmatic.com",
        now=_CANARY_NOW,
    )

    assert session.playbook.last_canary_at == _CANARY_NOW


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (DomainState.PROFILE_CANARY, DomainState.ACTIVE),
        (DomainState.ACTIVE, DomainState.DEGRADED),
        (DomainState.ACTIVE, DomainState.QUARANTINED),
        (DomainState.UNKNOWN, DomainState.UNSUPPORTED),
    ],
)
def test_non_canary_edges_leave_last_canary_at_untouched(from_state, to_state):
    session = _StubSession(_playbook("shop.example", from_state))
    session.playbook.last_canary_at = _CANARY_NOW
    transition(
        session,
        "shop.example",
        to_state,
        evidence={"x": 1},
        approver="ops@crawmatic.com",
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    # Unchanged -- a non-canary transition never rewrites canary history.
    assert session.playbook.last_canary_at == _CANARY_NOW


def test_last_canary_at_defaults_to_wall_clock_when_now_is_not_passed():
    before = datetime.now(timezone.utc)
    session = _StubSession(_playbook("shop.example", DomainState.UNKNOWN))
    transition(
        session,
        "shop.example",
        DomainState.DIRECT_CANARY,
        evidence={"canary": "scheduled"},
        approver="ops@crawmatic.com",
    )
    after = datetime.now(timezone.utc)
    assert before <= session.playbook.last_canary_at <= after


def test_profile_owner_is_recorded_onto_the_playbook_when_passed():
    session = _StubSession(_playbook("shop.example", DomainState.UNKNOWN))
    assert session.playbook.profile_owner is None
    transition(
        session,
        "shop.example",
        DomainState.DIRECT_CANARY,
        evidence={"canary": "scheduled"},
        approver="ops@crawmatic.com",
        profile_owner="crawl-team@crawmatic.com",
    )
    assert session.playbook.profile_owner == "crawl-team@crawmatic.com"


def test_omitting_profile_owner_never_clears_an_existing_owner():
    """An ownership change is an explicit act -- never an accident of
    omitting a kwarg on an unrelated transition."""
    session = _StubSession(_playbook("shop.example", DomainState.ACTIVE))
    session.playbook.profile_owner = "crawl-team@crawmatic.com"
    transition(session, "shop.example", DomainState.DEGRADED, evidence={"x": 1})
    assert session.playbook.profile_owner == "crawl-team@crawmatic.com"


def test_transition_never_touches_profile_fields():
    """``profile_fields`` is owner-populated and opaque to this module."""
    session = _StubSession(_playbook("shop.example", DomainState.ACTIVE))
    session.playbook.profile_fields = {"url_patterns": ["https://shop.example/p/*"]}
    transition(session, "shop.example", DomainState.DEGRADED, evidence={"x": 1})
    assert session.playbook.profile_fields == {
        "url_patterns": ["https://shop.example/p/*"]
    }
