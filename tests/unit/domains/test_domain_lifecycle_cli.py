"""Unit tests for ``scripts/domain_lifecycle.py`` (EPA W4 gate-review
follow-up F2, 2026-08-26).

The gate review's finding was that ``app_shared.domains.lifecycle
.transition`` -- the single writer of ``domain_playbooks.state`` and the
only appender to ``domain_lifecycle_audit`` -- had **no production
caller**. The CLI is that caller, so what these tests prove is the
*plumbing*: that CLI arguments reach the library unchanged, that every
guard the library owns still applies through the CLI (it is never worked
around), and that the commit/rollback decision matches the outcome.

Offline: a fake session (the same ``execute(...).scalar_one_or_none()``
+ ``add()`` surface ``test_lifecycle.py`` stubs, plus ``commit``/
``rollback`` recording) is injected via ``main(session_factory=...)``, so
no database, no environment variables, and no DSN are involved anywhere
in this module.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.domain_lifecycle import (  # noqa: E402
    cmd_transition,
    main,
    parse_args,
    parse_evidence,
)

from app_shared.models.domain_playbooks import (  # noqa: E402
    DomainLifecycleAudit,
    DomainPlaybook,
    DomainState,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        if self._obj is None:
            return []
        return self._obj if isinstance(self._obj, list) else [self._obj]


class _FakeSession:
    """Minimal ``Session`` surface the CLI touches, and nothing more."""

    def __init__(self, result=None):
        self.result = result
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    # context-manager, exactly like a real ``sessionmaker()`` call
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, stmt):
        return _FakeResult(self.result)

    def add(self, instance) -> None:
        self.added.append(instance)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _playbook(domain: str, state: DomainState, profile_version: int = 1) -> DomainPlaybook:
    return DomainPlaybook(
        domain=domain,
        preferred_access_method="DIRECT_HTTP",
        state=state,
        profile_version=profile_version,
    )


def _factory(session: _FakeSession):
    return lambda: session


# --------------------------------------------------------------------------
# 1. Argument parsing
# --------------------------------------------------------------------------


def test_parse_args_transition_carries_every_library_argument() -> None:
    args = parse_args(
        [
            "transition",
            "amazon.sa",
            "ACTIVE",
            "--evidence",
            '{"recert": "5/5"}',
            "--approver",
            "ops@crawmatic.com",
            "--profile-owner",
            "crawl-team@crawmatic.com",
        ]
    )
    assert args.command == "transition"
    assert args.domain == "amazon.sa"
    assert args.to_state == "ACTIVE"
    assert args.approver == "ops@crawmatic.com"
    assert args.profile_owner == "crawl-team@crawmatic.com"
    assert args.dry_run is False


def test_to_state_choices_are_exactly_the_domain_state_enum() -> None:
    """No hand-maintained list of state names the enum could outgrow."""
    for state in DomainState:
        args = parse_args(["transition", "x.example", state.value, "--evidence", "e"])
        assert args.to_state == state.value

    with pytest.raises(SystemExit):
        parse_args(["transition", "x.example", "NOT_A_STATE", "--evidence", "e"])


def test_evidence_is_mandatory_at_the_argument_layer_too() -> None:
    with pytest.raises(SystemExit):
        parse_args(["transition", "x.example", "DEGRADED"])


def test_parse_evidence_accepts_json_objects_and_free_text() -> None:
    assert parse_evidence('{"reason": "spike", "n": 3}') == {"reason": "spike", "n": 3}
    assert parse_evidence("failure signal spike") == {"note": "failure signal spike"}
    # A JSON scalar/array is not an object -- wrapped, not rejected.
    assert parse_evidence("42") == {"note": "42"}
    assert parse_evidence('["a"]') == {"note": '["a"]'}


def test_parse_evidence_leaves_empty_input_empty_for_the_library_to_refuse() -> None:
    """The CLI never invents evidence to satisfy the library's guard."""
    assert parse_evidence("   ") == {}


# --------------------------------------------------------------------------
# 2. The arguments actually reach transition()
# --------------------------------------------------------------------------


def test_main_transition_plumbs_arguments_into_the_library_and_commits() -> None:
    session = _FakeSession(_playbook("noon.com", DomainState.DEGRADED, profile_version=3))
    exit_code = main(
        [
            "transition",
            "noon.com",
            "ACTIVE",
            "--evidence",
            '{"recert": "21/21"}',
            "--approver",
            "ops@crawmatic.com",
            "--profile-owner",
            "crawl-team@crawmatic.com",
        ],
        session_factory=_factory(session),
    )

    assert exit_code == 0
    # Exactly one audit row, built by the library -- not by the CLI.
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, DomainLifecycleAudit)
    assert row.domain == "noon.com"
    assert row.from_state == DomainState.DEGRADED.value
    assert row.to_state == DomainState.ACTIVE.value
    assert row.evidence == {"recert": "21/21"}
    assert row.approver == "ops@crawmatic.com"
    assert row.profile_version == 4
    # Side effects the library owns still happened through the CLI.
    assert session.result.state is DomainState.ACTIVE
    assert session.result.profile_owner == "crawl-team@crawmatic.com"
    assert session.commits == 1
    assert session.rollbacks == 0


def test_main_transition_into_a_canary_state_stamps_last_canary_at() -> None:
    session = _FakeSession(_playbook("shop.example", DomainState.UNKNOWN))
    before = datetime.now(timezone.utc)
    exit_code = main(
        [
            "transition",
            "shop.example",
            "DIRECT_CANARY",
            "--evidence",
            "planned 5 direct fetches",
            "--approver",
            "ops@crawmatic.com",
        ],
        session_factory=_factory(session),
    )
    after = datetime.now(timezone.utc)

    assert exit_code == 0
    assert before <= session.result.last_canary_at <= after
    assert session.added[0].evidence == {"note": "planned 5 direct fetches"}


# --------------------------------------------------------------------------
# 3. Library guards are never worked around by the CLI
# --------------------------------------------------------------------------


def test_capability_granting_edge_without_approver_is_refused_not_committed() -> None:
    """The W4 gate-review finding, reached through the CLI: ``UNKNOWN ->
    DIRECT_CANARY`` grants broad crawl + expensive escalation, so the CLI
    cannot perform it without ``--approver`` either. Refused politely
    (exit 1), nothing added, nothing committed."""
    session = _FakeSession(_playbook("brand-new.example", DomainState.UNKNOWN))
    exit_code = main(
        [
            "transition",
            "brand-new.example",
            "DIRECT_CANARY",
            "--evidence",
            '{"canary": "planned"}',
        ],
        session_factory=_factory(session),
    )

    assert exit_code == 1
    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.result.state is DomainState.UNKNOWN


def test_illegal_edge_is_refused_politely() -> None:
    session = _FakeSession(_playbook("example.com", DomainState.QUARANTINED))
    exit_code = main(
        [
            "transition",
            "example.com",
            "ACTIVE",
            "--evidence",
            '{"x": 1}',
            "--approver",
            "ops@crawmatic.com",
        ],
        session_factory=_factory(session),
    )
    assert exit_code == 1
    assert session.commits == 0


def test_missing_evidence_is_refused_by_the_library_not_papered_over() -> None:
    session = _FakeSession(_playbook("example.com", DomainState.ACTIVE))
    exit_code = main(
        ["transition", "example.com", "DEGRADED", "--evidence", "   "],
        session_factory=_factory(session),
    )
    assert exit_code == 1
    assert session.added == []
    assert session.commits == 0


def test_unknown_domain_is_refused_politely() -> None:
    session = _FakeSession(None)
    exit_code = main(
        ["transition", "never-seen.example", "DIRECT_CANARY", "--evidence", "x"],
        session_factory=_factory(session),
    )
    assert exit_code == 1
    assert session.commits == 0


def test_refusal_message_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    session = _FakeSession(_playbook("example.com", DomainState.ACTIVE))
    main(
        ["transition", "example.com", "UNKNOWN", "--evidence", '{"x": 1}'],
        session_factory=_factory(session),
    )
    captured = capsys.readouterr()
    assert "refused:" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------
# 4. --dry-run rolls back a transition that would have succeeded
# --------------------------------------------------------------------------


def test_dry_run_runs_every_guard_then_rolls_back() -> None:
    session = _FakeSession(_playbook("example.com", DomainState.ACTIVE))
    exit_code = cmd_transition(
        session,
        domain="example.com",
        to_state="DEGRADED",
        evidence={"reason": "failure signal spike"},
        approver=None,
        profile_owner=None,
        dry_run=True,
    )
    assert exit_code == 0
    assert session.commits == 0
    assert session.rollbacks == 1


# --------------------------------------------------------------------------
# 5. Read-only subcommands
# --------------------------------------------------------------------------


def test_list_reports_state_version_owner_and_canary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    playbook = _playbook("amazon.sa", DomainState.ACTIVE, profile_version=7)
    playbook.profile_owner = "crawl-team@crawmatic.com"
    playbook.last_canary_at = datetime(2026, 8, 26, 3, 30, tzinfo=timezone.utc)
    session = _FakeSession([playbook])

    assert main(["list"], session_factory=_factory(session)) == 0
    out = capsys.readouterr().out
    assert "amazon.sa" in out
    assert "ACTIVE" in out
    assert "crawl-team@crawmatic.com" in out
    assert "2026-08-26T03:30:00+00:00" in out
    # Read-only: no write of any kind.
    assert session.added == []
    assert session.commits == 0


def test_history_prints_edges_oldest_first_with_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _audit(from_state, to_state, version, when, approver=None):
        row = DomainLifecycleAudit(
            domain="amazon.sa",
            from_state=from_state.value,
            to_state=to_state.value,
            evidence={"n": version},
            approver=approver,
            profile_version=version,
        )
        row.created_at = when
        return row

    newer = _audit(
        DomainState.DEGRADED,
        DomainState.ACTIVE,
        3,
        datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
        approver="ops@crawmatic.com",
    )
    older = _audit(
        DomainState.ACTIVE,
        DomainState.DEGRADED,
        2,
        datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc),
    )
    # The query orders newest-first (so --limit means "most recent N");
    # the output reverses it so the trail reads as a story.
    session = _FakeSession([newer, older])

    assert main(["history", "amazon.sa"], session_factory=_factory(session)) == 0
    out = capsys.readouterr().out
    assert out.index("ACTIVE -> DEGRADED") < out.index("DEGRADED -> ACTIVE")
    assert "ops@crawmatic.com" in out
    assert '"n": 2' in out
    assert session.commits == 0


def test_history_of_a_domain_with_no_trail_is_not_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession(None)
    assert main(["history", "never-seen.example"], session_factory=_factory(session)) == 0
    assert "no domain_lifecycle_audit rows" in capsys.readouterr().out
