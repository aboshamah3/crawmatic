"""Unit tests for the `workspace_entitlements` seeder + its refresh cadence.

Covers `libs/shared/app_shared/costauth/entitlements.py` and
`scripts/seed_workspace_entitlements.py` (EPA go-live prep, 2026-08-26).

**Why this seeder exists at all** is the thing these tests must keep
true: `CostAuthorizationService._check_entitlement` denies ALL paid work
for a workspace with no `workspace_entitlements` row, a non-`ACTIVE`
state, or evidence older than
`DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS` (86400). The table has no
production writer, so an unseeded engine scrapes nothing — and a seeded
engine that is not refreshed scrapes nothing 24 hours later.

Run against a real (SQLite) session rather than a fake one: unlike
`fleet_cost_budgets` (whose `JSONB` column does not compile under SQLite
-- see `tests/unit/test_seed_fleet_budget_cap.py` for the fake-session
substitute that forces), `workspaces` and `workspace_entitlements` are
plain `Text`/`Uuid`/`TIMESTAMPTZ`/enum-as-`VARCHAR` tables that SQLite
renders exactly. So the `LIKE 'seeded-%'` predicate that decides which
rows the refresher owns, and the "is there already a row for this
workspace" lookup that makes the seed idempotent, are both evaluated by a
real database here — not asserted against a canned rowcount.

The one thing SQLite cannot do is `SET TRANSACTION READ ONLY` (Postgres
syntax), so the dry-run guard is exercised through a thin recording
wrapper that intercepts exactly that statement and passes everything else
through to the real session. That keeps the ORDER assertion honest — the
guard has to be the FIRST statement or it does not govern the
transaction it is meant to (the lesson `scripts/backfill_daily_rollups.py`
learned against live Postgres).
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_backfill_daily_rollups.py` uses.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app_shared.costauth.entitlements import (  # noqa: E402
    SEEDED_EVIDENCE_VERSION_PREFIX,
    is_seeded_evidence_version,
    refresh_seeded_entitlements,
    seed_workspace_entitlements,
    seeded_evidence_version,
)
from app_shared.costauth.service import (  # noqa: E402
    DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS,
)
from app_shared.enums import WorkspaceStatus  # noqa: E402
from app_shared.models.cost_authorization import (  # noqa: E402
    EntitlementState,
    WorkspaceEntitlement,
)
from app_shared.models.identity import Workspace  # noqa: E402
from scripts.seed_workspace_entitlements import format_report, run_seed  # noqa: E402

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(hours=6)
_SEEDED = seeded_evidence_version(date(2026, 8, 26))


# --- fixtures ----------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite holding `workspaces` + `workspace_entitlements`.

    `StaticPool` so every `Session` the tests (and `run_seed`'s
    one-session-per-run factory) open shares ONE connection — without it
    SQLAlchemy hands each connection its own private in-memory database
    and a second session sees an empty schema.

    SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`
    (it is not set here), so `workspaces.default_scrape_profile_id` ->
    `scrape_profiles` never needs that table to exist.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Workspace.metadata.create_all(
        engine, tables=[Workspace.__table__, WorkspaceEntitlement.__table__]
    )
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _add_workspace(session: Session, slug: str) -> uuid.UUID:
    workspace = Workspace(
        name=slug.title(), slug=slug, status=WorkspaceStatus.ACTIVE
    )
    session.add(workspace)
    session.flush()
    return workspace.id


def _add_entitlement(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    evidence_version: str | None,
    state: EntitlementState = EntitlementState.ACTIVE,
    observed_at: datetime | None = None,
) -> None:
    session.add(
        WorkspaceEntitlement(
            workspace_id=workspace_id,
            state=state,
            evidence_version=evidence_version,
            observed_at=observed_at or (_NOW - timedelta(days=30)),
        )
    )
    session.flush()


def _observed_at(row: WorkspaceEntitlement) -> datetime:
    """`row.observed_at` as an aware UTC instant.

    `TZDateTime` round-trips as a real `TIMESTAMPTZ` on Postgres but
    SQLite has no timezone-aware storage type, so a value written aware
    comes back naive here. Re-attaching UTC keeps the assertions about
    the INSTANT (which is what the staleness gate compares) rather than
    about a storage detail of the test's database.
    """
    value = row.observed_at
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _entitlement(session: Session, workspace_id: uuid.UUID) -> WorkspaceEntitlement:
    return session.execute(
        select(WorkspaceEntitlement).where(
            WorkspaceEntitlement.workspace_id == workspace_id
        )
    ).scalar_one()


# --- the evidence-version seam ------------------------------------------


def test_seeded_evidence_version_is_dated_and_prefixed() -> None:
    assert seeded_evidence_version(date(2026, 8, 26)) == "seeded-2026-08-26"
    assert seeded_evidence_version(date(2026, 8, 26)).startswith(
        SEEDED_EVIDENCE_VERSION_PREFIX
    )


def test_only_the_prefix_counts_as_seeded() -> None:
    """The ownership marker, in both directions. A version this module did
    not write must never be recognised as its own -- that recognition is
    the ONLY thing stopping the refresher from fighting the future real
    SaaS->engine ingest over the same rows."""
    assert is_seeded_evidence_version("seeded-2026-08-26") is True
    assert is_seeded_evidence_version("seeded-") is True
    assert is_seeded_evidence_version("saas-billing-v7") is False
    assert is_seeded_evidence_version("preseeded-2026-08-26") is False
    assert is_seeded_evidence_version(None) is False


def test_seed_refuses_an_evidence_version_the_refresher_cannot_recognise(
    db_session,
) -> None:
    """A row stamped with a foreign version would be invisible to the
    refresh cadence and would go stale-deny within 24h. Raise instead of
    writing rows nothing will maintain."""
    _add_workspace(db_session, "acme")
    with pytest.raises(ValueError, match="seeded-"):
        seed_workspace_entitlements(
            db_session, now=_NOW, evidence_version="saas-billing-v7", apply=True
        )


# --- seeding ------------------------------------------------------------


def test_seed_creates_an_active_fresh_row_for_every_workspace(db_session) -> None:
    ws_a = _add_workspace(db_session, "acme")
    ws_b = _add_workspace(db_session, "beta")

    report = seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=True
    )
    db_session.flush()

    assert set(report.created) == {ws_a, ws_b}
    assert report.refreshed == ()
    assert report.left_alone == ()
    assert report.workspaces_seen == 2

    for workspace_id in (ws_a, ws_b):
        row = _entitlement(db_session, workspace_id)
        assert row.state is EntitlementState.ACTIVE
        assert row.evidence_version == _SEEDED
        assert _observed_at(row) == _NOW


def test_seed_is_idempotent_and_restamps_its_own_rows(db_session) -> None:
    """The contract the operator relies on: running it twice must not
    create a second row, and the second run must move `observed_at`
    forward (that is what stops the gate's staleness clock)."""
    workspace_id = _add_workspace(db_session, "acme")

    seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=True
    )
    db_session.flush()

    later_version = seeded_evidence_version(date(2026, 8, 27))
    report = seed_workspace_entitlements(
        db_session, now=_LATER, evidence_version=later_version, apply=True
    )
    db_session.flush()

    assert report.created == ()
    assert report.refreshed == (workspace_id,)
    rows = list(session_rows(db_session))
    assert len(rows) == 1, "a second seed run must not create a second row"
    assert _observed_at(rows[0]) == _LATER
    assert rows[0].evidence_version == later_version


def session_rows(session: Session):
    return session.execute(select(WorkspaceEntitlement)).scalars()


def test_seed_never_overwrites_a_row_it_does_not_own(db_session) -> None:
    """THE guard. A row written by the real SaaS->engine ingest (or by an
    operator) carries a foreign `evidence_version`; the seeder must leave
    every field of it exactly as found and report the workspace, because
    real billing evidence always beats this module's placeholder."""
    workspace_id = _add_workspace(db_session, "acme")
    real_observed_at = _NOW - timedelta(days=10)
    _add_entitlement(
        db_session,
        workspace_id,
        evidence_version="saas-billing-v7",
        state=EntitlementState.PAST_DUE,
        observed_at=real_observed_at,
    )

    report = seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=True
    )
    db_session.flush()

    assert report.left_alone == (workspace_id,)
    assert report.created == ()
    assert report.refreshed == ()

    row = _entitlement(db_session, workspace_id)
    assert row.evidence_version == "saas-billing-v7"
    assert row.state is EntitlementState.PAST_DUE
    assert _observed_at(row) == real_observed_at


def test_a_null_evidence_version_row_is_foreign_not_seeded(db_session) -> None:
    """An untagged row is one this module did not write. "I don't
    recognise this" must always resolve to "leave it alone"."""
    workspace_id = _add_workspace(db_session, "acme")
    _add_entitlement(db_session, workspace_id, evidence_version=None)

    report = seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=True
    )

    assert report.left_alone == (workspace_id,)
    assert _entitlement(db_session, workspace_id).evidence_version is None


def test_seed_classifies_all_three_buckets_in_one_pass(db_session) -> None:
    fresh = _add_workspace(db_session, "fresh")
    seeded = _add_workspace(db_session, "seeded")
    foreign = _add_workspace(db_session, "foreign")
    _add_entitlement(db_session, seeded, evidence_version="seeded-2026-08-01")
    _add_entitlement(db_session, foreign, evidence_version="saas-billing-v7")

    report = seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=True
    )

    assert report.created == (fresh,) or set(report.created) == {fresh}
    assert report.refreshed == (seeded,)
    assert report.left_alone == (foreign,)
    assert report.workspaces_seen == 3


def test_seed_with_apply_false_writes_nothing_but_still_classifies(db_session) -> None:
    workspace_id = _add_workspace(db_session, "acme")

    report = seed_workspace_entitlements(
        db_session, now=_NOW, evidence_version=_SEEDED, apply=False
    )
    db_session.flush()

    assert report.created == (workspace_id,)
    assert list(session_rows(db_session)) == []


# --- the refresh cadence's write ----------------------------------------


def test_refresh_restamps_only_the_rows_the_seeder_owns(db_session) -> None:
    """The whole point of the `seeded-` prefix, proven against a real
    `LIKE` evaluation: the placeholder's clock is reset, the real ingest's
    is not touched at all."""
    seeded_ws = _add_workspace(db_session, "seeded")
    real_ws = _add_workspace(db_session, "real")
    untagged_ws = _add_workspace(db_session, "untagged")
    stale = _NOW - timedelta(days=5)
    _add_entitlement(db_session, seeded_ws, evidence_version=_SEEDED, observed_at=stale)
    _add_entitlement(
        db_session, real_ws, evidence_version="saas-billing-v7", observed_at=stale
    )
    _add_entitlement(db_session, untagged_ws, evidence_version=None, observed_at=stale)

    refreshed = refresh_seeded_entitlements(db_session, now=_LATER)
    db_session.flush()
    db_session.expire_all()

    assert refreshed == 1
    assert _observed_at(_entitlement(db_session, seeded_ws)) == _LATER
    assert _observed_at(_entitlement(db_session, real_ws)) == stale
    assert _observed_at(_entitlement(db_session, untagged_ws)) == stale


def test_refresh_leaves_a_control_plane_numeric_version_alone(db_session) -> None:
    """EPA C2: the SaaS control plane stamps `evidence_version` as
    `str(int)` (`"7"`), never with the `seeded-` prefix. A refresher that
    touched those rows would move `observed_at` off the SaaS's `as_of`
    and make evidence the SaaS never sent look fresh — the exact failure
    the prefix exists to prevent. Proven against a real `LIKE`."""
    replicated_ws = _add_workspace(db_session, "replicated")
    zero_version_ws = _add_workspace(db_session, "zero-version")
    stale = _NOW - timedelta(days=5)
    _add_entitlement(db_session, replicated_ws, evidence_version="7", observed_at=stale)
    _add_entitlement(db_session, zero_version_ws, evidence_version="0", observed_at=stale)

    assert refresh_seeded_entitlements(db_session, now=_LATER) == 0
    db_session.flush()
    db_session.expire_all()

    assert _observed_at(_entitlement(db_session, replicated_ws)) == stale
    assert _observed_at(_entitlement(db_session, zero_version_ws)) == stale


def test_refresh_restamps_a_seeded_row_whatever_its_state(db_session) -> None:
    """`observed_at` is a freshness fact, not an authorization one -- the
    gate still denies a non-ACTIVE row. Skipping suspended rows would let
    an operator's deliberate suspension decay into the WRONG denial
    reason ("stale") in their face."""
    workspace_id = _add_workspace(db_session, "suspended")
    _add_entitlement(
        db_session,
        workspace_id,
        evidence_version=_SEEDED,
        state=EntitlementState.SUSPENDED,
        observed_at=_NOW - timedelta(days=5),
    )

    assert refresh_seeded_entitlements(db_session, now=_LATER) == 1
    db_session.expire_all()
    row = _entitlement(db_session, workspace_id)
    assert _observed_at(row) == _LATER
    assert row.state is EntitlementState.SUSPENDED


def test_refresh_on_an_empty_table_is_a_no_op(db_session) -> None:
    assert refresh_seeded_entitlements(db_session, now=_NOW) == 0


def test_refresh_interval_leaves_real_margin_under_the_staleness_deadline() -> None:
    """The cadence and the deadline must not be the same number. At 6h the
    fleet survives three consecutive missed ticks (a redeploy, a broker
    hiccup, a night) before ANY workspace starts denying paid work."""
    from app_shared.config import Settings

    interval = Settings.model_fields["ENTITLEMENT_REFRESH_INTERVAL_SECONDS"].default
    assert interval * 4 <= DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS, (
        "the refresh cadence must fit at least four times inside the gate's "
        "max evidence age, or a single missed tick can deny the fleet"
    )


# --- the script wrapper -------------------------------------------------


class _GuardRecordingSession:
    """A real `Session`, plus a record of every statement, minus the guard.

    `SET TRANSACTION READ ONLY` is Postgres syntax SQLite rejects, so it
    is intercepted here rather than executed — but its POSITION is
    recorded, which is the property that actually matters: the guard only
    governs the current transaction if nothing has established one first.
    """

    def __init__(self, inner: Session) -> None:
        self.inner = inner
        self.statements: list[str] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        rendered = str(statement)
        self.statements.append(rendered)
        if "SET TRANSACTION READ ONLY" in rendered:
            return None
        return self.inner.execute(statement, *args, **kwargs)

    def close(self) -> None:
        """Deliberately NOT delegated: the test owns the shared session's
        lifetime, and closing it here would detach the rows the
        assertions then read."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_run_seed_dry_run_issues_the_read_only_guard_first_and_writes_nothing(
    db_session,
) -> None:
    _add_workspace(db_session, "acme")
    db_session.commit()
    recorder = _GuardRecordingSession(db_session)

    report = run_seed(
        now=_NOW,
        evidence_version=_SEEDED,
        apply=False,
        session_factory=lambda: recorder,
    )

    assert "SET TRANSACTION READ ONLY" in recorder.statements[0], (
        "the guard must be the FIRST statement on the session or it does not "
        "govern the transaction the pass runs in"
    )
    assert len(report.created) == 1
    assert list(session_rows(db_session)) == []


def test_run_seed_apply_commits_and_never_issues_the_guard(db_session) -> None:
    workspace_id = _add_workspace(db_session, "acme")
    db_session.commit()
    recorder = _GuardRecordingSession(db_session)

    report = run_seed(
        now=_NOW,
        evidence_version=_SEEDED,
        apply=True,
        session_factory=lambda: recorder,
    )

    assert not any("READ ONLY" in stmt for stmt in recorder.statements)
    assert report.created == (workspace_id,)
    db_session.expire_all()
    assert _entitlement(db_session, workspace_id).evidence_version == _SEEDED


def test_format_report_names_every_workspace_it_refused_to_touch() -> None:
    """A left-alone workspace needs a follow-up (real ingest? hand-edit?),
    so its id is printed in full rather than merely counted."""
    from app_shared.costauth.entitlements import EntitlementSeedReport

    workspace_id = uuid.uuid4()
    text_out = format_report(
        EntitlementSeedReport(left_alone=(workspace_id,)), apply=False
    )
    assert "DRY-RUN" in text_out
    assert str(workspace_id) in text_out
    assert "left_alone=1" in text_out
