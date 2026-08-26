"""Two-plane limits + weighted fair queuing in the scheduler (EPA W4.2, §6).

Five claims, one test each — the contract's own Step-1 list:

1. ``test_fleet_domain_limit_is_enforced_across_tenants`` — two workspaces
   on one merchant share that merchant's cap, and one tenant's in-flight
   work (read from C3's own live reservations) blocks another's.
2. ``test_noisy_tenant_cannot_starve_others`` — a workspace with a large
   backlog of *older* due rules cannot consume the whole pass; the quiet
   tenant's rules all run.
3. ``test_poison_rule_is_dead_lettered_without_aborting_the_pass`` — an
   always-raising rule is isolated, dead-lettered (disabled + announced
   through the EXISTING webhook consumer), and every other tenant's work
   in the same pass still runs.
4. ``test_freshness_urgency_cannot_bypass_fleet_or_authorization`` —
   urgency reorders and nothing else: an urgent item on a capped domain is
   deferred, and an urgent item whose C3 authorization is denied is NOT
   dispatched.
5. ``test_bounded_retries_then_dead_letter_then_replay`` — exactly
   ``max_attempts`` attempts, then a dead letter, then replay tooling puts
   it back.

Why these are integration tests
-------------------------------
Three of the five are statements about a *database*: "the domain counter
is shared across tenants" is a statement about a cross-tenant aggregate
over ``cost_reservations``; "the poison rule is dead-lettered" is a
statement about ``refresh_rules.enabled`` and an ``outbox_messages`` row;
"authorization denies" is a statement about C3 running against real
entitlement evidence. None of them survives being asserted against a fake
session, so this suite runs the real wiring against a real PostgreSQL
migrated to the real alembic head.

Scratch database, never the developer's
---------------------------------------
The module brings up its OWN throwaway ``postgres:18-alpine`` container on
a dedicated port and migrates it with ``alembic -x db_url=...``. It never
reads ``.env``, never touches ``DATABASE_URL``/``MIGRATION_DATABASE_URL``,
and removes the container it created (by name) on teardown. It SKIPS
cleanly when Docker is unavailable. Same pattern, deliberately, as
``tests/integration/test_cost_authorization.py``.

The scratch container's superuser is used, so forced RLS is bypassed and
these tests exercise the SCHEDULER's logic rather than the policy — RLS
has its own dedicated suites.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]

from app_shared.costauth.service import (  # noqa: E402
    CostAuthorizationService,
    period_key_for,
)
from app_shared.domains.state_lookup import reset_domain_state_cache  # noqa: E402
from app_shared.enums import CompetitorStatus, ScrapeScope  # noqa: E402
from app_shared.models.competitors_matches import Competitor  # noqa: E402
from app_shared.models.cost_authorization import (  # noqa: E402
    AuthorizationPurpose,
    CostBudget,
    CostReservation,
    EntitlementState,
    ReservationState,
    WorkspaceEntitlement,
)
from app_shared.models.domain_playbooks import DomainPlaybook, DomainState  # noqa: E402
from app_shared.models.outbox import OutboxMessage  # noqa: E402
from app_shared.models.proxy_breaker import (  # noqa: E402
    GLOBAL_BREAKER_SCOPE,
    ProxyBreakerState,
    ProxyCircuitBreaker,
)
from app_shared.models.refresh_rules import RefreshRule  # noqa: E402
from app_shared.scheduling.fair_queue import (  # noqa: E402
    DeferralReason,
    FairShare,
    FleetLimits,
    RetryLedger,
    ScheduleCandidate,
    plan_pass,
)

def _load_scheduler_app():
    """Import ``apps/scheduler``'s ``app.scheduler.scheduler_app``, safely.

    ``apps/api``, ``apps/workers`` and ``apps/scheduler`` each ship their
    own top-level ``app`` package and all three are on ``sys.path``, with
    ``apps/api`` first — so a bare ``import app.scheduler`` in the shared
    test process resolves to the API's package and fails. The repository's
    unit tests solve this by running the scheduler in a subprocess; this
    suite needs the module in-process (it drives real database fixtures),
    so it instead puts ``apps/scheduler`` at the FRONT of ``sys.path`` for
    exactly the duration of this one import and then puts everything back.

    Restoring matters: leaving ``apps/scheduler`` in front would silently
    repoint ``app`` for every other test module in the same session, and
    ``tests/integration`` is full of modules that import the API's ``app``.
    """
    scheduler_root = str(REPO_ROOT / "apps" / "scheduler")
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, scheduler_root)
    try:
        from app.scheduler import scheduler_app as module

        return module
    finally:
        sys.path[:] = saved_path
        for name in [
            n for n in list(sys.modules) if n == "app" or n.startswith("app.")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


scheduler_app = _load_scheduler_app()

# A port and a container name nothing else in this repository uses, so a
# stale container from another suite can never be mistaken for this one's
# (and so this suite can never delete another's).
_PG_IMAGE = "postgres:18-alpine"
_PG_CONTAINER = "cm-w42-fairqueue-pg"
_PG_PORT = 55498
_PG_PASSWORD = "fairqueue-scratch"  # noqa: S105 - throwaway container, never a secret
_PG_DB = "crawmatic_fairqueue"

#: Domains used below. All three are seeded ``ACTIVE`` in C2's rule table
#: so no test is accidentally measuring domain certification.
SHARED_DOMAIN = "shared-merchant.example"
QUIET_DOMAIN = "quiet-merchant.example"
OTHER_DOMAIN = "other-merchant.example"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode
            == 0
        )
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="No Docker daemon — this suite provisions its own scratch Postgres",
)


def _dsn() -> str:
    return f"postgresql+psycopg://postgres:{_PG_PASSWORD}@127.0.0.1:{_PG_PORT}/{_PG_DB}"


@pytest.fixture(scope="module")
def engine():
    """A scratch Postgres migrated to alembic head. Removed on teardown."""
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)
    up = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", _PG_CONTAINER,
            "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
            "-e", f"POSTGRES_DB={_PG_DB}",
            "-p", f"127.0.0.1:{_PG_PORT}:5432",
            _PG_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start {_PG_IMAGE}: {up.stderr.strip()[:200]}")

    try:
        eng = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                eng = create_engine(_dsn(), pool_size=10, max_overflow=10)
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                if eng is not None:
                    eng.dispose()
                eng = None
                time.sleep(1)
        if eng is None:
            pytest.skip("scratch Postgres never became reachable")

        migrate = subprocess.run(
            ["uv", "run", "alembic", "-x", f"db_url={_dsn()}", "upgrade", "head"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert migrate.returncode == 0, migrate.stderr[-4000:]

        yield eng
        eng.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


@pytest.fixture()
def sessions(engine):
    """A ``sessionmaker`` + a clean slate for each test."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE refresh_rules, competitor_product_matches, competitors, "
                "scrape_job_targets, scrape_jobs, cost_reservations, cost_budgets, "
                "fleet_cost_budgets, workspace_entitlements, outbox_messages, "
                "network_operations, domain_playbooks, proxy_circuit_breakers, "
                "workspaces RESTART IDENTITY CASCADE"
            )
        )
    reset_domain_state_cache()
    scheduler_app._FAIR_QUEUE_LEDGER = None
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def _scope(factory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _factory(sessions):
    """A zero-argument session factory of the shape the pass expects."""
    return sessions


def _seed_fleet_evidence(sessions, now: datetime) -> None:
    """The FLEET-scoped rows every workspace shares: breaker + playbooks.

    Seeded wide open so no test is accidentally measuring the breaker or
    domain certification — each test closes exactly one gate.
    """
    with _scope(sessions) as session:
        session.execute(text("DELETE FROM proxy_circuit_breakers"))
        session.execute(text("DELETE FROM domain_playbooks"))
        session.add(
            ProxyCircuitBreaker(
                id=uuid.uuid4(),
                scope_key=GLOBAL_BREAKER_SCOPE,
                state=ProxyBreakerState.CLOSED,
                evaluated_at=now,
                trip_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        for domain in (SHARED_DOMAIN, QUIET_DOMAIN, OTHER_DOMAIN):
            session.add(
                DomainPlaybook(
                    id=uuid.uuid4(),
                    domain=domain,
                    preferred_access_method="DIRECT_HTTP",
                    method_templates=[],
                    state=DomainState.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    reset_domain_state_cache()


def _seed_workspace(
    sessions,
    now: datetime,
    *,
    label: str,
    entitlement: EntitlementState = EntitlementState.ACTIVE,
    entitlement_age_seconds: int = 0,
) -> uuid.UUID:
    """One workspace plus its durable entitlement evidence."""
    workspace_id = uuid.uuid4()
    with _scope(sessions) as session:
        session.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', now(), now())"
            ),
            {"id": workspace_id, "name": label, "slug": f"{label}-{workspace_id.hex[:10]}"},
        )
        session.add(
            WorkspaceEntitlement(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                state=entitlement,
                plan_code="pro",
                evidence_version="saas-v7",
                observed_at=now - timedelta(seconds=entitlement_age_seconds),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            CostBudget(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                period_key=period_key_for(now),
                currency="USD",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return workspace_id


def _competitor(sessions, workspace_id: uuid.UUID, domain: str, now: datetime) -> uuid.UUID:
    competitor_id = uuid.uuid4()
    with _scope(sessions) as session:
        session.add(
            Competitor(
                id=competitor_id,
                workspace_id=workspace_id,
                name=domain,
                domain=domain,
                status=CompetitorStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return competitor_id


def _rule(
    sessions,
    workspace_id: uuid.UUID,
    *,
    now: datetime,
    competitor_id: uuid.UUID | None = None,
    overdue_seconds: int = 60,
    interval_minutes: int = 60,
    priority: int = 0,
    name: str = "rule",
) -> uuid.UUID:
    """One due `refresh_rules` row.

    ``competitor_id`` set -> a COMPETITOR-scope rule, whose domain the
    fleet plane can name. ``None`` -> a WORKSPACE-scope rule, which spans
    domains and is therefore a wildcard candidate.
    """
    rule_id = uuid.uuid4()
    with _scope(sessions) as session:
        session.add(
            RefreshRule(
                id=rule_id,
                workspace_id=workspace_id,
                name=name,
                scope=(
                    ScrapeScope.COMPETITOR if competitor_id else ScrapeScope.WORKSPACE
                ),
                competitor_id=competitor_id,
                interval_minutes=interval_minutes,
                priority=priority,
                enabled=True,
                next_run_at=now - timedelta(seconds=overdue_seconds),
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return rule_id


def _reserve_live(
    sessions,
    workspace_id: uuid.UUID,
    domain: str,
    now: datetime,
    *,
    count: int = 1,
) -> None:
    """``count`` LIVE C3 grants on ``domain`` — the occupancy the cap sees."""
    with _scope(sessions) as session:
        for _ in range(count):
            session.add(
                CostReservation(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    authorization_id=uuid.uuid4(),
                    state=ReservationState.RESERVED,
                    purpose=AuthorizationPurpose.REFRESH,
                    domain=domain,
                    transport="PROXY",
                    provider="proxy",
                    budget_decision_version="test",
                    currency="USD",
                    reserved_cost_minor_units=1,
                    reserved_bytes=1,
                    reserved_requests=1,
                    reserved_browser_seconds=0,
                    # Long lease: "live" means RESERVED *and* unexpired, so
                    # a test that advances its clock between passes must
                    # hold a lease that is still live at the later moment.
                    lease_expires_at=now + timedelta(hours=24),
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()


def _rule_row(sessions, rule_id: uuid.UUID) -> RefreshRule:
    with _scope(sessions) as session:
        return session.execute(
            select(RefreshRule).where(RefreshRule.id == rule_id)
        ).scalars().one()


def _open_gate(_candidate) -> None:
    """A gate that authorizes everything — for tests not about the gate."""
    return None


# ---------------------------------------------------------------------------
# 1. Fleet/domain limit, ACROSS tenants
# ---------------------------------------------------------------------------


def test_fleet_domain_limit_is_enforced_across_tenants(sessions):
    """One merchant, one cap — no matter how many workspaces want it.

    This is the workspace-scoped multiplication report §6 names: with a
    per-workspace cap of 1 both of these rules would run, and the merchant
    would see two concurrent fetches. The cap is a property of the DOMAIN,
    so exactly one runs; the loser is deferred, not failed.

    The second half is the stronger claim: a live grant held by a THIRD
    workspace — read from C3's own ``cost_reservations``, not from a
    counter of the scheduler's own — fills the domain and blocks both.
    """
    now = datetime.now(timezone.utc)
    _seed_fleet_evidence(sessions, now)
    ws_a = _seed_workspace(sessions, now, label="a")
    ws_b = _seed_workspace(sessions, now, label="b")
    ws_c = _seed_workspace(sessions, now, label="c")
    rule_a = _rule(
        sessions, ws_a, now=now, competitor_id=_competitor(sessions, ws_a, SHARED_DOMAIN, now)
    )
    rule_b = _rule(
        sessions, ws_b, now=now, competitor_id=_competitor(sessions, ws_b, SHARED_DOMAIN, now)
    )

    one_per_domain = FleetLimits(default_domain_concurrency=1, fleet_concurrency=100)
    ledger = RetryLedger(max_attempts=3)

    outcome = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=now,
        batch_limit=10,
        ledger=ledger,
        limits=one_per_domain,
        gate=_open_gate,
    )

    # Two due rules, two DIFFERENT tenants, ONE domain -> one dispatch.
    assert len(outcome.plan.admitted) + len(outcome.plan.deferred) == 2
    assert len(outcome.dispatched) == 1
    assert outcome.plan.deferrals_for(DeferralReason.DOMAIN_LIMIT)
    winner, loser = (
        (rule_a, rule_b) if str(rule_a) in outcome.dispatched else (rule_b, rule_a)
    )
    assert str(loser) not in outcome.dispatched
    # The deferred rule is untouched: same schedule, still enabled, still
    # a candidate next pass. A deferral is not a failure.
    loser_row = _rule_row(sessions, loser)
    assert loser_row.enabled is True
    assert loser_row.last_run_at is None
    assert _rule_row(sessions, winner).last_run_at is not None

    # Now a THIRD workspace holds the domain's only slot in C3's ledger.
    # Nothing the scheduler itself recorded — real live grants.
    _reserve_live(sessions, ws_c, SHARED_DOMAIN, now, count=1)
    with _scope(sessions) as session:
        usage = scheduler_app.read_fleet_domain_usage(session, now)
    assert usage.domain(SHARED_DOMAIN) == 1

    later = now + timedelta(hours=2)
    blocked = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=later,
        batch_limit=10,
        ledger=ledger,
        limits=one_per_domain,
        gate=_open_gate,
    )
    assert blocked.dispatched == ()
    assert len(blocked.plan.deferrals_for(DeferralReason.DOMAIN_LIMIT)) >= 1


# ---------------------------------------------------------------------------
# 2. Weighted fair share
# ---------------------------------------------------------------------------


def test_noisy_tenant_cannot_starve_others(sessions):
    """A big, OLDER backlog must not consume the pass.

    The noisy tenant's rules are all more overdue than the quiet tenant's,
    so a FIFO pass ordered by ``next_run_at`` (which is what
    ``run_refresh_pass`` does) would hand it all ten slots and the quiet
    tenant would wait for a quiet minute that never comes. Deficit round
    robin gives every workspace a turn per round, so the quiet tenant's
    entire (small) backlog clears in the same pass.
    """
    now = datetime.now(timezone.utc)
    _seed_fleet_evidence(sessions, now)
    noisy = _seed_workspace(sessions, now, label="noisy")
    quiet = _seed_workspace(sessions, now, label="quiet")

    # WORKSPACE-scope rules: multi-domain, so the fleet plane's per-domain
    # cap is deliberately out of the picture and this test measures only
    # the tenant plane.
    noisy_rules = [
        _rule(sessions, noisy, now=now, overdue_seconds=3600 + i, name=f"noisy-{i}")
        for i in range(20)
    ]
    quiet_rules = [
        _rule(sessions, quiet, now=now, overdue_seconds=60 + i, name=f"quiet-{i}")
        for i in range(3)
    ]

    outcome = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=now,
        batch_limit=10,
        ledger=RetryLedger(),
        limits=FleetLimits(default_domain_concurrency=1, fleet_concurrency=1000),
        share=FairShare(),
        gate=_open_gate,
    )

    dispatched = set(outcome.dispatched)
    assert len(dispatched) == 10
    quiet_done = [r for r in quiet_rules if str(r) in dispatched]
    noisy_done = [r for r in noisy_rules if str(r) in dispatched]
    # Every quiet rule ran despite being the newest work in the pass...
    assert len(quiet_done) == 3
    # ...and the noisy tenant took the rest, not the lot.
    assert len(noisy_done) == 7

    # Weights are relative, not absolute: doubling the noisy tenant's
    # weight changes its SHARE of a round, and the quiet tenant still is
    # not starved.
    later = now + timedelta(hours=2)
    weighted = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=later,
        batch_limit=6,
        ledger=RetryLedger(),
        limits=FleetLimits(default_domain_concurrency=1, fleet_concurrency=1000),
        share=FairShare(weights={str(noisy): 2.0}),
        gate=_open_gate,
    )
    weighted_quiet = [r for r in quiet_rules if str(r) in set(weighted.dispatched)]
    weighted_noisy = [r for r in noisy_rules if str(r) in set(weighted.dispatched)]
    assert len(weighted.dispatched) == 6
    assert len(weighted_noisy) == 4
    assert len(weighted_quiet) == 2


# ---------------------------------------------------------------------------
# 3. Poison isolation
# ---------------------------------------------------------------------------


def test_poison_rule_is_dead_lettered_without_aborting_the_pass(sessions, monkeypatch):
    """One always-raising rule stops itself, and nothing else.

    ``run_refresh_pass``'s except branch ``break``s the whole pass, so one
    poison rule froze every other tenant's scheduling until a human found
    it. Here the failure is isolated, the rule is dead-lettered — durably:
    ``enabled = false`` plus an ``outbox_messages`` row on the EXISTING
    ``webhook_events.create_webhook_event`` consumer — and the other two
    tenants' rules dispatch in the same pass.
    """
    now = datetime.now(timezone.utc)
    _seed_fleet_evidence(sessions, now)
    ws_a = _seed_workspace(sessions, now, label="a")
    ws_b = _seed_workspace(sessions, now, label="b")
    poison_competitor = _competitor(sessions, ws_a, SHARED_DOMAIN, now)
    poison_rule = _rule(sessions, ws_a, now=now, competitor_id=poison_competitor)
    healthy_a = _rule(sessions, ws_a, now=now, name="healthy-a")
    healthy_b = _rule(sessions, ws_b, now=now, name="healthy-b")

    real_create = scheduler_app.create_scope_job

    def exploding_create_scope_job(session, *, target_id=None, **kwargs):
        if target_id == poison_competitor:
            raise RuntimeError("poison rule: scope resolution blew up")
        return real_create(session, target_id=target_id, **kwargs)

    monkeypatch.setattr(scheduler_app, "create_scope_job", exploding_create_scope_job)

    ledger = RetryLedger(
        max_attempts=1,
        on_dead_letter=lambda record: scheduler_app.record_dead_letter(
            _factory(sessions), record
        ),
    )
    outcome = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=now,
        batch_limit=10,
        ledger=ledger,
        limits=FleetLimits(default_domain_concurrency=4, fleet_concurrency=100),
        gate=_open_gate,
    )

    # The pass did NOT abort: both healthy rules ran.
    assert set(outcome.dispatched) == {str(healthy_a), str(healthy_b)}
    assert outcome.dead_lettered == (str(poison_rule),)
    assert ledger.is_dead_lettered(str(poison_rule))

    # Durable half 1: the rule is disabled, so a restart cannot resurrect it.
    assert _rule_row(sessions, poison_rule).enabled is False

    # Durable half 2: announced through the EXISTING consumer, never a new
    # unregistered task name (EPA B7).
    with _scope(sessions) as session:
        messages = session.execute(select(OutboxMessage)).scalars().all()
    dead_letter_messages = [
        m
        for m in messages
        if m.payload.get("event_type") == scheduler_app.SCHEDULER_DEAD_LETTER_EVENT_TYPE
    ]
    assert len(dead_letter_messages) == 1
    message = dead_letter_messages[0]
    assert message.task_name == "webhook_events.create_webhook_event"
    assert message.queue == "webhook_events"
    assert message.payload["payload"]["refresh_rule_id"] == str(poison_rule)
    assert "poison rule" in message.payload["payload"]["last_error"]


# ---------------------------------------------------------------------------
# 4. Urgency orders; it does not open gates
# ---------------------------------------------------------------------------


def test_freshness_urgency_cannot_bypass_fleet_or_authorization(sessions):
    """Urgency wins the ORDER and loses every argument with a limit.

    Three claims in one test because they are one property:

    a) within a tenant's queue the more-overdue rule is planned FIRST
       (urgency does order — ACROSS tenants the tenant plane orders, which
       is what fair queuing means);
    b) that same urgent rule is DEFERRED when its domain is at the fleet
       cap — urgency cannot raise a cap;
    c) an urgent rule whose C3 authorization is denied — here by a real
       ``CostAuthorizationService`` against real, cancelled entitlement
       evidence — is NOT dispatched. Ordering happens before
       authorization; authorization is still the only gate that decides.
    """
    now = datetime.now(timezone.utc)
    _seed_fleet_evidence(sessions, now)
    # A workspace whose entitlement was cancelled: C3 denies it.
    starving = _seed_workspace(
        sessions, now, label="starving", entitlement=EntitlementState.CANCELLED
    )
    healthy = _seed_workspace(sessions, now, label="healthy")

    urgent = _rule(
        sessions,
        starving,
        now=now,
        competitor_id=_competitor(sessions, starving, SHARED_DOMAIN, now),
        overdue_seconds=86_400,   # a full day past a 15-minute cadence
        interval_minutes=15,
        name="urgent",
    )
    relaxed = _rule(
        sessions,
        starving,
        now=now,
        competitor_id=_competitor(sessions, starving, QUIET_DOMAIN, now),
        overdue_seconds=30,
        interval_minutes=1440,
        name="relaxed",
    )
    healthy_rule = _rule(
        sessions,
        healthy,
        now=now,
        competitor_id=_competitor(sessions, healthy, OTHER_DOMAIN, now),
        overdue_seconds=60,
        name="healthy",
    )

    with _scope(sessions) as session:
        candidates = scheduler_app.load_due_candidates(session, now=now, limit=100)
    assert {c.key for c in candidates} == {str(urgent), str(relaxed), str(healthy_rule)}
    limits = FleetLimits(default_domain_concurrency=4, fleet_concurrency=100)

    # (a) Ordering, within the tenant: with room for exactly one of this
    #     workspace's items, urgency picks the day-late 15-minute rule over
    #     the 30-seconds-late daily one.
    starving_candidates = [c for c in candidates if c.workspace_id == str(starving)]
    ordered = plan_pass(starving_candidates, now=now, batch_limit=1, limits=limits)
    assert ordered.admitted_keys == (str(urgent),)

    # (b) Fleet safety beats urgency: another tenant fills SHARED_DOMAIN.
    _reserve_live(sessions, healthy, SHARED_DOMAIN, now, count=4)
    with _scope(sessions) as session:
        usage = scheduler_app.read_fleet_domain_usage(session, now)
    assert usage.domain(SHARED_DOMAIN) == 4
    capped = plan_pass(candidates, now=now, batch_limit=10, limits=limits, usage=usage)
    assert str(urgent) not in capped.admitted_keys
    assert capped.deferrals_for(DeferralReason.DOMAIN_LIMIT)[0].key == str(urgent)
    # The less urgent rule, on an uncapped domain, sails past it. Urgency
    # buys order, never a slot.
    assert str(relaxed) in capped.admitted_keys
    assert str(healthy_rule) in capped.admitted_keys

    # (c) Authorization beats urgency: clear the domain, run the real pass
    #     with C3 as the gate. The urgent rule IS admitted (nothing in the
    #     scheduler plane stops it now) and then DENIED, so it never fires.
    with _scope(sessions) as session:
        session.execute(text("DELETE FROM cost_reservations"))
        session.commit()

    def scope():
        return _scope(sessions)

    service = CostAuthorizationService(scope, system_session_scope=scope)

    def costauth_gate(candidate):
        service.assert_entitled(candidate.workspace_id)
        return None

    outcome = scheduler_app.run_fair_scheduling_pass(
        _factory(sessions),
        now=now,
        batch_limit=10,
        ledger=RetryLedger(),
        limits=limits,
        gate=costauth_gate,
    )

    admitted = list(outcome.plan.admitted_keys)
    assert set(admitted) == {str(urgent), str(relaxed), str(healthy_rule)}
    # Urgency ordered it ahead of its own tenant's other rule...
    assert admitted.index(str(urgent)) < admitted.index(str(relaxed))
    # ...and it still did not run.
    assert str(urgent) not in outcome.dispatched
    assert set(outcome.denied) == {
        (str(urgent), "ENTITLEMENT_INACTIVE"),
        (str(relaxed), "ENTITLEMENT_INACTIVE"),
    }
    assert outcome.dispatched == (str(healthy_rule),)
    # A denial is not a failure: nothing was retried, nothing dead-lettered,
    # and the rule keeps its schedule for when the account is paid again.
    assert outcome.retried == ()
    assert outcome.dead_lettered == ()
    assert _rule_row(sessions, urgent).last_run_at is None
    assert _rule_row(sessions, urgent).enabled is True


# ---------------------------------------------------------------------------
# 5. Bounded retries -> dead letter -> replay
# ---------------------------------------------------------------------------


def test_bounded_retries_then_dead_letter_then_replay(sessions, monkeypatch):
    """Exactly ``max_attempts`` attempts, then park it; replay puts it back.

    Bounded is the operative word in both directions: a transient failure
    must not dead-letter on its first bad minute, and a permanent one must
    not be retried forever. Then the parked item must be *recoverable* —
    a dead letter nobody can replay is a deletion with extra steps.
    """
    now = datetime.now(timezone.utc)
    _seed_fleet_evidence(sessions, now)
    workspace = _seed_workspace(sessions, now, label="ws")
    poison_competitor = _competitor(sessions, workspace, SHARED_DOMAIN, now)
    poison_rule = _rule(sessions, workspace, now=now, competitor_id=poison_competitor)
    healthy_rule = _rule(sessions, workspace, now=now, name="healthy")

    exploding = {"on": True}
    real_create = scheduler_app.create_scope_job

    def maybe_exploding(session, *, target_id=None, **kwargs):
        if exploding["on"] and target_id == poison_competitor:
            raise RuntimeError("still poison")
        return real_create(session, target_id=target_id, **kwargs)

    monkeypatch.setattr(scheduler_app, "create_scope_job", maybe_exploding)

    ledger = RetryLedger(
        max_attempts=3,
        on_dead_letter=lambda record: scheduler_app.record_dead_letter(
            _factory(sessions), record
        ),
    )
    limits = FleetLimits(default_domain_concurrency=4, fleet_concurrency=100)

    def one_pass(moment):
        return scheduler_app.run_fair_scheduling_pass(
            _factory(sessions),
            now=moment,
            batch_limit=10,
            ledger=ledger,
            limits=limits,
            gate=_open_gate,
        )

    # Attempts 1 and 2: retried, still enabled, still a candidate.
    for attempt, offset_hours in ((1, 0), (2, 2)):
        outcome = one_pass(now + timedelta(hours=offset_hours))
        assert outcome.retried == (str(poison_rule),), attempt
        assert outcome.dead_lettered == ()
        assert ledger.attempts_for(str(poison_rule)) == attempt
        assert _rule_row(sessions, poison_rule).enabled is True

    # Attempt 3 hits the bound: dead-lettered, disabled, announced.
    final = one_pass(now + timedelta(hours=4))
    assert final.dead_lettered == (str(poison_rule),)
    assert ledger.is_dead_lettered(str(poison_rule))
    assert _rule_row(sessions, poison_rule).enabled is False
    records = ledger.dead_letters()
    assert [r.key for r in records] == [str(poison_rule)]
    assert records[0].attempts == 3
    assert records[0].domain == SHARED_DOMAIN

    # The healthy rule ran in every one of those passes — isolation held.
    healthy_row = _rule_row(sessions, healthy_rule)
    assert healthy_row.last_run_at is not None

    # A parked item is not scheduled even if it somehow reappears as a
    # candidate (a row someone re-enabled by hand, a replica with a stale
    # read): the ledger filters it out of the plan by key, not by row.
    resurrected = ScheduleCandidate(
        key=str(poison_rule),
        workspace_id=str(workspace),
        domain=SHARED_DOMAIN,
        due_at=now,
    )
    plan = plan_pass(
        [resurrected], now=now, batch_limit=10, limits=limits, ledger=ledger
    )
    assert plan.admitted_keys == ()
    assert plan.deferrals_for(DeferralReason.DEAD_LETTERED)[0].key == str(poison_rule)

    # Replay tooling: fix the cause, replay, and it runs again.
    exploding["on"] = False
    replayed = scheduler_app.replay_dead_letters(_factory(sessions), ledger)
    assert replayed == [str(poison_rule)]
    assert ledger.dead_letters() == ()
    assert ledger.attempts_for(str(poison_rule)) == 0
    assert _rule_row(sessions, poison_rule).enabled is True

    after_replay = one_pass(now + timedelta(hours=6))
    assert str(poison_rule) in after_replay.dispatched
    assert _rule_row(sessions, poison_rule).last_run_at is not None
