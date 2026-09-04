"""``CostAuthorizationService`` against a real Postgres (EPA C3, READY-006).

The C3 contract is a set of claims about a **database**, not about Python:
"twenty concurrent authorizations against a hundred-cent budget produce
exactly ten grants" is a statement about ``SELECT ... FOR UPDATE``, and
"an expired lease does not release a live operation" is a statement about
a join against C1's ledger. Neither survives being asserted against a
fake session, so this suite runs the real service against a real
PostgreSQL migrated to the real alembic head.

Scratch database, never the developer's
---------------------------------------
The module brings up its OWN throwaway ``postgres:18-alpine`` container on
a dedicated port and migrates it with ``alembic -x db_url=...``. It never
reads ``.env``, never touches ``DATABASE_URL``/``MIGRATION_DATABASE_URL``,
and never connects to anything the developer configured — the ``-x``
override exists precisely so a one-off run can name a URL that is not in
the settings (see ``alembic/env.py::_resolve_db_url``). The container and
its volume are removed on teardown.

It SKIPS cleanly (never fails) when Docker is unavailable, so the suite is
collectable in a build environment with no daemon.

Roles: the scratch container's superuser is used, so forced RLS is
bypassed and these tests exercise the SERVICE's logic rather than the
policy. That is deliberate division of labour — RLS behaviour has its own
dedicated suites (``test_rls_behavior.py``, ``test_tenant_isolation_roles.py``,
``test_rls_cross_workspace.py``) which test it properly, with the
non-superuser runtime roles, against a database provisioned by
``provision_db_roles.py``. Asserting it a fourth time here, badly, would
add no coverage.

The eight tests are the contract's own Step-1 list, implemented verbatim
in substance:

1. ``test_denies_on_stale_breaker`` -> ``BREAKER_EVIDENCE_STALE``
2. ``test_denies_on_unknown_domain_broad_crawl`` -> ``DOMAIN_NOT_CERTIFIED``
3. ``test_hard_budget_cannot_be_exceeded_under_concurrency`` -> 10/10
4. ``test_all_dimensions_reserved_atomically`` -> ``BYTE_BUDGET_EXCEEDED``
5. ``test_lease_expiry_does_not_release_live_operation`` -> stays RESERVED
6. ``test_settle_is_cas_and_idempotent`` -> 96
7. ``test_cancelled_workspace_gets_denied_from_durable_evidence`` ->
   ``ENTITLEMENT_INACTIVE``
8. ``test_warning_thresholds_emit_events`` -> 50/75/90 dedup keys
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app_shared.costauth.service import (
    AuthorizationPurpose,
    AuthorizationRequest,
    CostAuthorizationDenied,
    CostAuthorizationService,
    DenialReason,
    SettledCost,
    period_key_for,
    sweep_expired_reservations,
)
from app_shared.domains.state_lookup import reset_domain_state_cache
from app_shared.models.cost_authorization import (
    CostBudget,
    CostReservation,
    EntitlementState,
    ReservationState,
    WorkspaceEntitlement,
)
from app_shared.models.domain_playbooks import DomainPlaybook, DomainState
from app_shared.models.network_operations import NetworkOperation, NetworkTransport
from app_shared.models.outbox import OutboxMessage
from app_shared.models.proxy_breaker import (
    GLOBAL_BREAKER_SCOPE,
    ProxyBreakerState,
    ProxyCircuitBreaker,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A port and a container name nothing else in this repository uses, so a
# stale container from another suite can never be mistaken for this one's
# (and so this suite can never delete another's).
_PG_IMAGE = "postgres:18-alpine"
_PG_CONTAINER = "cm-c3-costauth-pg"
_PG_PORT = 55497
_PG_PASSWORD = "costauth-scratch"  # noqa: S105 - throwaway container, never a real secret
_PG_DB = "crawmatic_costauth"

#: The certified domain most tests authorize against (`ACTIVE` in C2's
#: rule table, so every gate but the one under test is open).
CERTIFIED_DOMAIN = "certified.example"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="No Docker daemon — this suite provisions its own scratch Postgres",
)


def _dsn() -> str:
    return (
        f"postgresql+psycopg://postgres:{_PG_PASSWORD}"
        f"@127.0.0.1:{_PG_PORT}/{_PG_DB}"
    )


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
                eng = create_engine(_dsn(), pool_size=30, max_overflow=10)
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

        # `-x db_url=` (alembic/env.py::_resolve_db_url) — the sanctioned
        # one-off override. No environment variable is written, so the
        # developer's own DSNs are untouched by this suite.
        migrate = subprocess.run(
            ["uv", "run", "alembic", "-x", f"db_url={_dsn()}", "upgrade", "head"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert migrate.returncode == 0, migrate.stderr[-4000:]

        yield eng
        eng.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


@pytest.fixture()
def sessions(engine):
    """A ``sessionmaker`` + a fresh, empty schema for each test.

    Truncating between tests (rather than re-migrating) keeps the suite
    fast while giving every test the same clean slate — important here
    because budget counters are cumulative by design and a leaked
    reservation from a previous test would silently change another's
    arithmetic.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE cost_reservations, cost_budgets, fleet_cost_budgets, "
                "workspace_entitlements, outbox_messages, network_operations, "
                "domain_playbooks, proxy_circuit_breakers, workspaces "
                "RESTART IDENTITY CASCADE"
            )
        )
    reset_domain_state_cache()
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def _scope_factory(factory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _service(sessions, *, workspace_id=None, **kwargs) -> CostAuthorizationService:
    """A service bound to the scratch database.

    Both seams point at the same engine: the scratch container has one
    superuser role, so the tenant/system distinction has no *role* to
    express here. The distinction it exists for — which code path is
    allowed to be cross-tenant — is a property of the call sites and is
    documented on them; this fixture only needs both to reach the data.
    """
    def scope():
        return _scope_factory(sessions)

    return CostAuthorizationService(
        scope, system_session_scope=scope, default_workspace_id=workspace_id, **kwargs
    )


def _seed(
    sessions,
    *,
    entitlement: EntitlementState = EntitlementState.ACTIVE,
    entitlement_age_seconds: int = 0,
    breaker_state: ProxyBreakerState = ProxyBreakerState.CLOSED,
    breaker_age_seconds: int = 0,
    domain_state: DomainState | None = DomainState.ACTIVE,
    limit_cost_micro_units: int | None = None,
    limit_bytes: int | None = None,
    max_concurrent: int | None = None,
) -> uuid.UUID:
    """Seed one workspace and every gate's evidence. Returns the workspace id.

    Everything a grant must clear is seeded OPEN by default, so each test
    closes exactly one gate and the failure it asserts can only have come
    from that gate.
    """
    now = datetime.now(timezone.utc)
    workspace_id = uuid.uuid4()
    with sessions() as session:
        # The breaker and the playbook are FLEET rows keyed by scope/domain,
        # not by workspace, so seeding a SECOND workspace in one test must
        # replace them rather than insert a duplicate (both carry a unique
        # key). Clearing first keeps `_seed` callable more than once —
        # which is what a test comparing two workspaces needs.
        session.execute(text("DELETE FROM proxy_circuit_breakers"))
        session.execute(text("DELETE FROM domain_playbooks"))
        session.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', now(), now())"
            ),
            {"id": workspace_id, "name": "c3", "slug": f"c3-{workspace_id.hex[:12]}"},
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
            ProxyCircuitBreaker(
                id=uuid.uuid4(),
                scope_key=GLOBAL_BREAKER_SCOPE,
                state=breaker_state,
                evaluated_at=now - timedelta(seconds=breaker_age_seconds),
                trip_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        if domain_state is not None:
            session.add(
                DomainPlaybook(
                    id=uuid.uuid4(),
                    domain=CERTIFIED_DOMAIN,
                    preferred_access_method="DIRECT_HTTP",
                    method_templates=[],
                    state=domain_state,
                    created_at=now,
                    updated_at=now,
                )
            )
        if (
            limit_cost_micro_units is not None
            or limit_bytes is not None
            or max_concurrent is not None
        ):
            session.add(
                CostBudget(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    period_key=period_key_for(now),
                    currency="USD",
                    limit_cost_micro_units=limit_cost_micro_units,
                    limit_bytes=limit_bytes,
                    max_concurrent_reservations=max_concurrent,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    return workspace_id


def _req(workspace_id: uuid.UUID, **overrides) -> AuthorizationRequest:
    """The contract's ``req(...)`` helper: a valid request with overrides."""
    kwargs = dict(
        workspace_id=workspace_id,
        domain=CERTIFIED_DOMAIN,
        transport="PROXY",
        provider="proxy",
        estimated_bytes=1_000,
        estimated_cost_micro_units=10,
        purpose=AuthorizationPurpose.REFRESH,
        estimated_requests=1,
    )
    kwargs.update(overrides)
    return AuthorizationRequest(**kwargs)


# ---------------------------------------------------------------------------
# 1. Breaker evidence is fail-closed
# ---------------------------------------------------------------------------


def test_denies_on_stale_breaker(sessions) -> None:
    """Stale breaker evidence DENIES — absent evidence is not permission.

    The binding posture for READY-006: a breaker row whose `evaluated_at`
    has gone quiet means the evaluator itself has stopped, which is
    exactly the condition under which "we have no idea what we are
    spending" is the honest answer. A gate that let work through here
    would be a gate that opens during precisely the incident it exists
    for.
    """
    workspace_id = _seed(sessions, breaker_age_seconds=7 * 24 * 3600)
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(
            _req(workspace_id, purpose=AuthorizationPurpose.BROWSER_ESCALATION)
        )

    assert excinfo.value.reason == DenialReason.BREAKER_EVIDENCE_STALE


def test_denies_when_the_breaker_row_is_missing_entirely(sessions) -> None:
    """No breaker row at all is the SAME denial as a stale one.

    Not in the contract's verbatim list, but it is the other half of the
    same fail-closed rule and the cheaper of the two to get wrong: a
    freshly-provisioned deployment has no breaker row, and "no row" is the
    state in which an unbounded run is most likely.
    """
    workspace_id = _seed(sessions)
    with sessions() as session:
        session.execute(text("DELETE FROM proxy_circuit_breakers"))
        session.commit()

    service = _service(sessions, workspace_id=workspace_id)
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id))

    assert excinfo.value.reason == DenialReason.BREAKER_EVIDENCE_STALE


# ---------------------------------------------------------------------------
# 2. Domain state (C2's rule table)
# ---------------------------------------------------------------------------


def test_denies_on_unknown_domain_broad_crawl(sessions) -> None:
    """An uncertified domain cannot be broad-crawled -> DOMAIN_NOT_CERTIFIED.

    `never-seen.example` has no `domain_playbooks` row, so C2 resolves it
    to `UNKNOWN`, whose rule permits only "a tiny DIRECT canary". A
    `REFRESH` on `PROXY` is not that, so it is refused under the
    contract's named reason.
    """
    workspace_id = _seed(sessions)
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id, domain="never-seen.example"))

    assert excinfo.value.reason == DenialReason.DOMAIN_NOT_CERTIFIED


def test_unknown_domain_still_permits_the_tiny_direct_canary(sessions) -> None:
    """...and the canary C2 carves out still gets through.

    The other half of the same rule, and the half that matters for
    liveness: if an uncertified domain could not be probed DIRECT, no
    domain could ever leave `UNKNOWN` and the certification pipeline would
    deadlock on its own safety check. This is the test that stops
    `DOMAIN_NOT_CERTIFIED` from being implemented as "deny everything".
    """
    workspace_id = _seed(sessions)
    service = _service(sessions, workspace_id=workspace_id)

    grant = service.authorize(
        _req(
            workspace_id,
            domain="never-seen.example",
            transport="DIRECT",
            purpose=AuthorizationPurpose.DISCOVERY,
        )
    )
    assert grant.authorization_id is not None


def test_quarantined_domain_denies_all_paid_work(sessions) -> None:
    """C2's `paid_allowed` gate gets its OWN reason, not DOMAIN_NOT_CERTIFIED.

    Pins C3's documented reconciliation of C2's three nested gates: a
    quarantined domain and an uncertified one demand completely different
    operator responses, so collapsing them onto one reason would throw
    away the only information the denial carries.
    """
    workspace_id = _seed(sessions, domain_state=DomainState.QUARANTINED)
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id))

    assert excinfo.value.reason == DenialReason.DOMAIN_QUARANTINED


# ---------------------------------------------------------------------------
# 3. The hard ceiling, under real concurrency
# ---------------------------------------------------------------------------


def test_hard_budget_cannot_be_exceeded_under_concurrency(sessions) -> None:
    """20 concurrent authorizations of 10 against a 100 budget -> 10 and 10.

    The claim the whole design exists to support, and the one no
    fake-session test can make: twenty real connections race for the same
    `cost_budgets` row, `SELECT ... FOR UPDATE` serializes them, and the
    eleventh finds no room. If the counter were a Redis `INCR` (or a
    read-then-write without the lock) this would over-grant — which is
    exactly what it did before C3.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)

    def attempt():
        try:
            return service.authorize(_req(workspace_id, estimated_cost_micro_units=10))
        except CostAuthorizationDenied as denial:
            return denial

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(20)))

    grants = [o for o in outcomes if not isinstance(o, Exception)]
    denials = [o for o in outcomes if isinstance(o, CostAuthorizationDenied)]

    assert len(grants) == 10, [d.reason.value for d in denials]
    assert len(denials) == 10
    assert {d.reason for d in denials} == {DenialReason.MONEY_BUDGET_EXCEEDED}
    assert service.remaining_budget_micro_units() == 0


# ---------------------------------------------------------------------------
# 4. All four dimensions, one transaction
# ---------------------------------------------------------------------------


def test_all_dimensions_reserved_atomically(sessions) -> None:
    """Money fits, bytes do not -> the WHOLE authorization is denied.

    And denied with the BYTE reason, not the money one: reporting the
    dimension that actually bound is the difference between an operator
    raising the right limit and raising the wrong one. Nothing is left
    reserved — the counters are untouched, which is what "one transaction"
    means in practice.
    """
    workspace_id = _seed(
        sessions, limit_cost_micro_units=10_000, limit_bytes=1_000_000
    )
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(
            _req(
                workspace_id,
                estimated_cost_micro_units=1,
                estimated_bytes=10**9,
            )
        )

    assert excinfo.value.reason == DenialReason.BYTE_BUDGET_EXCEEDED
    # The money dimension must NOT have been reserved on the way past.
    assert service.remaining_budget_micro_units() == 10_000
    with sessions() as session:
        assert session.execute(select(CostReservation)).first() is None


# ---------------------------------------------------------------------------
# 5. Leases, not TTLs
# ---------------------------------------------------------------------------


def test_lease_expiry_does_not_release_live_operation(sessions) -> None:
    """An expired lease over an OPEN ledger operation stays RESERVED.

    The single most important property of the sweeper. An expired lease is
    evidence that a *heartbeat* stopped — a worker paused, throttled, or
    merely slow — and treating it as evidence that the *work* stopped is
    how a budget gets spent twice while its counters look healthy. The
    sweeper must consult C1's ledger, and this proves it does.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    with sessions() as session:
        # The operation C4 will open under this grant: present, and OPEN
        # (`closed_at IS NULL`) — the exact predicate C1's own immutability
        # trigger reads.
        session.add(
            NetworkOperation(
                id=uuid.uuid4(),
                network_request_id=uuid.uuid4(),
                canonical_url_hash="hash",
                domain=CERTIFIED_DOMAIN,
                provider="proxy",
                transport=NetworkTransport.PROXY,
                authorization_id=grant.authorization_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.execute(
            text(
                "UPDATE cost_reservations SET lease_expires_at = now() - interval '1 hour'"
            )
        )
        session.commit()

    with sessions() as session:
        released, skipped = sweep_expired_reservations(session)

    assert (released, skipped) == (0, 1)
    with sessions() as session:
        state = session.execute(
            select(CostReservation.state).where(
                CostReservation.authorization_id == grant.authorization_id
            )
        ).scalar_one()
    assert state is ReservationState.RESERVED
    assert service.remaining_budget_micro_units() == 90


def test_lease_expiry_releases_when_no_operation_ever_opened(sessions) -> None:
    """...and the same sweep DOES reap a grant nothing ever dispatched.

    The other side of the same check. Without it "never release a live
    operation" could be implemented as "never release", and a crashed
    worker's money would be stranded until someone noticed.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    with sessions() as session:
        session.execute(
            text(
                "UPDATE cost_reservations SET lease_expires_at = now() - interval '1 hour'"
            )
        )
        session.commit()

    with sessions() as session:
        released, skipped = sweep_expired_reservations(session)

    assert (released, skipped) == (1, 0)
    with sessions() as session:
        state = session.execute(
            select(CostReservation.state).where(
                CostReservation.authorization_id == grant.authorization_id
            )
        ).scalar_one()
    assert state is ReservationState.RELEASED
    assert service.remaining_budget_micro_units() == 100


# ---------------------------------------------------------------------------
# 6. Settlement is compare-and-set
# ---------------------------------------------------------------------------


def test_settle_is_cas_and_idempotent(sessions) -> None:
    """Settling 4 of a 10-unit hold twice leaves 96, not 92.

    At-least-once delivery is the norm here (`task_acks_late` +
    the outbox), so the second settle is not a hypothetical. The CAS on
    the reservation's own state is what makes the replay a no-op rather
    than a second debit.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    service.settle(grant.authorization_id, SettledCost(cost_micro_units=4))
    service.settle(grant.authorization_id, SettledCost(cost_micro_units=4))  # replay

    assert service.remaining_budget_micro_units() == 96


def test_release_is_cas_and_idempotent(sessions) -> None:
    """Releasing twice returns the hold once. Same guard, other transition."""
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    service.release(grant.authorization_id)
    service.release(grant.authorization_id)

    assert service.remaining_budget_micro_units() == 100


# ---------------------------------------------------------------------------
# 7. Entitlement, from durable local evidence
# ---------------------------------------------------------------------------


def test_cancelled_workspace_gets_denied_from_durable_evidence(sessions) -> None:
    """A cancelled workspace is refused from the engine's OWN evidence.

    No SaaS call is made anywhere in this path — that is the requirement.
    The denial must hold during exactly the outage that would make asking
    impossible, which is why the evidence is a local table rather than an
    API call.
    """
    workspace_id = _seed(
        sessions, entitlement=EntitlementState.CANCELLED, limit_cost_micro_units=100
    )
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id))

    assert excinfo.value.reason == DenialReason.ENTITLEMENT_INACTIVE


def test_assert_entitled_is_the_account_level_gate_for_fan_out_routes(sessions) -> None:
    """`assert_entitled` denies an inactive account and reserves nothing.

    The fan-out routes (`POST /v1/variants/{id}/rescrape`,
    `POST /v1/jobs/run/variant/{id}`) span several competitor domains, so
    they cannot honestly price one grant — but they still owe the caller
    the one denial that depends on no domain. This pins both halves: it
    refuses a cancelled workspace, and it takes no reservation while
    doing so (a route that "checked" by reserving would charge a user for
    pressing a button).
    """
    workspace_id = _seed(
        sessions, entitlement=EntitlementState.CANCELLED, limit_cost_micro_units=100
    )
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.assert_entitled()

    assert excinfo.value.reason == DenialReason.ENTITLEMENT_INACTIVE
    with sessions() as session:
        assert session.execute(select(CostReservation)).first() is None
    assert service.remaining_budget_micro_units() == 100

    # ...and it is silent for a live account. `_seed` clears the fleet
    # rows it owns, so the domain-state cache must be dropped with them or
    # the second workspace would be judged against the first's cached
    # playbook.
    active_workspace = _seed(sessions)
    reset_domain_state_cache()
    _service(sessions, workspace_id=active_workspace).assert_entitled()


def test_stale_entitlement_evidence_is_treated_as_inactive(sessions) -> None:
    """Stale evidence denies exactly as a cancelled account does (W1.1).

    "Staleness treated as inactive" is W1.1's stated contract, and it is
    the half that is easy to leave out: a reconciler that stops running
    leaves every workspace's row saying ACTIVE forever, so freshness has
    to be part of the predicate rather than an assumption about it.
    """
    workspace_id = _seed(
        sessions, entitlement_age_seconds=30 * 86_400, limit_cost_micro_units=100
    )
    service = _service(sessions, workspace_id=workspace_id)

    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id))

    assert excinfo.value.reason == DenialReason.ENTITLEMENT_INACTIVE
    assert "stale" in excinfo.value.detail.lower()


# ---------------------------------------------------------------------------
# 8. Warning thresholds
# ---------------------------------------------------------------------------


def test_warning_thresholds_emit_events(sessions) -> None:
    """50/75/90% crossings appear once each, as outbox dedup keys.

    Also pins the B7 outbox rule: the messages name
    `webhook_events.create_webhook_event`, an ALREADY-REGISTERED consumer
    (`apps/workers/app/workers/tasks_webhooks.py`). An outbox row naming a
    task nothing consumes is a message that looks delivered and never is,
    so the task name is asserted here rather than left to review.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)

    for _ in range(9):
        service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    with sessions() as session:
        messages = list(session.execute(select(OutboxMessage)).scalars().all())

    dedup_keys = [m.dedup_key or "" for m in messages]
    assert any("budget-warn:50" in key for key in dedup_keys), dedup_keys
    assert any("budget-warn:75" in key for key in dedup_keys), dedup_keys
    assert any("budget-warn:90" in key for key in dedup_keys), dedup_keys

    workspace_keys = [k for k in dedup_keys if f"workspace:{workspace_id}" in k]
    assert len(workspace_keys) == len(set(workspace_keys)) == 3, workspace_keys
    assert {m.task_name for m in messages} == {"webhook_events.create_webhook_event"}
    assert {m.payload["event_type"] for m in messages} == {"budget.threshold.warning"}


# ---------------------------------------------------------------------------
# 9. Cancellation's step 4 is real (A2 completion)
# ---------------------------------------------------------------------------


def test_release_reservations_for_job_releases_the_jobs_grants(sessions) -> None:
    """A2's `release_reservations_for_job` returns a cancelled job's money.

    The stub it replaces returned 0 unconditionally, which meant a
    cancelled job's budget stayed held until its lease lapsed. This is the
    assertion that makes cancellation's "supported" certification true.
    """
    from app_shared.costauth.service import release_reservations_for_scrape_job

    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    job_id = uuid.uuid4()
    service.authorize(
        _req(workspace_id, estimated_cost_micro_units=10, scrape_job_id=job_id)
    )
    service.authorize(
        _req(workspace_id, estimated_cost_micro_units=10, scrape_job_id=job_id)
    )
    assert service.remaining_budget_micro_units() == 80

    with sessions() as session:
        released = release_reservations_for_scrape_job(
            session, workspace_id=workspace_id, scrape_job_id=job_id
        )
        session.commit()
    assert released == 2
    assert service.remaining_budget_micro_units() == 100

    # Idempotent: a re-run of a crashed cancellation releases nothing more.
    with sessions() as session:
        assert (
            release_reservations_for_scrape_job(
                session, workspace_id=workspace_id, scrape_job_id=job_id
            )
            == 0
        )
        session.commit()
    assert service.remaining_budget_micro_units() == 100


# ---------------------------------------------------------------------------
# 10. Dedupe / coalescing + concurrency cap
# ---------------------------------------------------------------------------


def test_redelivery_with_the_same_dedupe_key_reuses_the_grant(sessions) -> None:
    """A redelivered request collapses onto the grant it already holds.

    The money-side twin of EPA B1's dispatch identity: the same batch
    delivered twice must cost the budget once, or an at-least-once broker
    becomes an at-least-once *spender*.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)

    first = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=10, dedupe_key="batch-a")
    )
    second = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=10, dedupe_key="batch-a")
    )

    assert second.authorization_id == first.authorization_id
    assert second.replayed is True
    assert service.remaining_budget_micro_units() == 90


def test_concurrency_cap_denies_past_the_live_grant_limit(sessions) -> None:
    """The cap counts LIVE grants, and denies with its own reason."""
    workspace_id = _seed(sessions, max_concurrent=2)
    service = _service(sessions, workspace_id=workspace_id)

    service.authorize(_req(workspace_id))
    service.authorize(_req(workspace_id))
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        service.authorize(_req(workspace_id))

    assert excinfo.value.reason == DenialReason.CONCURRENCY_CAP_EXCEEDED


# ---------------------------------------------------------------------------
# 11. ONE grant, MANY operations — EPA Phase C F1
# ---------------------------------------------------------------------------
#
# The gate review's finding, and the reason this section exists: a batch
# grant is minted ONCE for up to `SCRAPE_DISPATCH_HTTP_BATCH_MAX` targets
# and the spider then opens one physical operation per target under it.
# While every close called the TERMINAL `settle()`, the first one settled
# the whole grant with one fetch's cost and released the rest of the hold
# — reserve 20, run 20 operations of 1 each, and the budget recorded a
# single unit spent and 99 still available.


def _accrue_operations(service, authorization_id, *, count: int, each: int) -> None:
    """Close ``count`` operations under one grant, each costing ``each``.

    Exactly what C4's recorder does per operation: `settle_partial`, never
    `settle` — the network boundary sees one fetch at a time and can never
    know it is holding the batch's last one.
    """
    for _ in range(count):
        service.settle_partial(
            authorization_id, SettledCost(cost_micro_units=each, requests=1)
        )


def test_many_operations_under_one_grant_settle_to_their_sum(sessions) -> None:
    """The reviewer's exact reproduction: 20 reserved, 20 ops of 1 -> 20.

    Before the fix this asserted `settled == 1` and `remaining == 99`,
    which is a ceiling that stops binding after the first fetch of a
    two-hundred-target batch — the single most expensive way for a budget
    to be wrong.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=20, estimated_requests=20)
    )

    _accrue_operations(service, grant.authorization_id, count=20, each=1)

    with sessions() as session:
        reservation = session.execute(
            select(CostReservation).where(
                CostReservation.authorization_id == grant.authorization_id
            )
        ).scalar_one()
        assert reservation.settled_cost_micro_units == 20
        assert reservation.settled_requests == 20
        # NON-terminal: the grant is still live, because a batch is not
        # over just because one of its operations closed.
        assert reservation.state is ReservationState.RESERVED

    # Settled totals equal the SUM of the operations' observed costs, and
    # the hold is fully drawn down rather than released early.
    assert service.remaining_budget_micro_units() == 80
    with sessions() as session:
        budget = session.execute(select(CostBudget)).scalar_one()
        assert budget.settled_cost_micro_units == 20
        assert budget.reserved_cost_micro_units == 0


def test_an_operation_overrunning_the_batch_estimate_is_real_spend(sessions) -> None:
    """Accruals past the hold add to spend; they never go negative.

    Over-running an estimate is ordinary (that is why settlement exists).
    What must not happen is a negative `reserved_*`, which would be a
    silent, permanent over-grant.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=5))

    _accrue_operations(service, grant.authorization_id, count=8, each=1)

    with sessions() as session:
        budget = session.execute(select(CostBudget)).scalar_one()
        assert budget.settled_cost_micro_units == 8
        assert budget.reserved_cost_micro_units == 0
    assert service.remaining_budget_micro_units() == 92


def test_terminal_settle_returns_only_the_residual_hold(sessions) -> None:
    """A grant that accrued 4 of a 10 hold returns 6, not 10.

    The complement of the accrual: together they return each reserved unit
    exactly once. This is also the shape a discovery run uses — accrue the
    ladder's rungs, then close the grant with what is left.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    _accrue_operations(service, grant.authorization_id, count=4, each=1)
    # Still 90: "remaining" is limit - (reserved + settled), and while the
    # grant is live 4 have moved from held to spent but the other 6 are
    # STILL HELD -- which is the whole point of reserving.
    assert service.remaining_budget_micro_units() == 90

    service.settle(grant.authorization_id, SettledCost(cost_micro_units=0, requests=0))

    assert service.remaining_budget_micro_units() == 96
    with sessions() as session:
        reservation = session.execute(select(CostReservation)).scalar_one()
        assert reservation.state is ReservationState.SETTLED
        assert reservation.settled_cost_micro_units == 4
        budget = session.execute(select(CostBudget)).scalar_one()
        assert budget.reserved_cost_micro_units == 0
        assert budget.settled_cost_micro_units == 4


def test_a_replayed_operation_close_accrues_nothing_twice(sessions) -> None:
    """C4's recorder accrues only when ITS close closed the ledger row.

    `settle_partial` accumulates, so it cannot be compare-and-set on its
    own; its idempotence is the ledger's `closed_at IS NULL` update, which
    the database decides. This proves the recorder honours that: a
    redelivered close of an already-closed operation moves no counter.
    """
    from app_shared.netledger.recorder import (
        NetLedgerRecorder,
        OperationIntent,
        OperationOutcome,
    )

    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id, estimated_cost_micro_units=10))

    recorder = NetLedgerRecorder(
        lambda: _scope_factory(sessions), costauth=service
    )
    intent = OperationIntent(
        url=f"https://{CERTIFIED_DOMAIN}/p/1",
        domain=CERTIFIED_DOMAIN,
        transport=NetworkTransport.PROXY,
        provider="proxy",
        workspace_id=workspace_id,
        authorization_id=grant.authorization_id,
    )
    nrid = recorder.open(intent)
    outcome = OperationOutcome(
        bytes_compressed=1_000,
        estimated_cost_micro_units=3,
        currency="USD",
        billing_unit="REQUEST",
    )

    first = recorder.close(nrid, outcome)
    assert first.settled is True
    # The redelivery: same operation, same outcome, closed again.
    second = recorder.close(nrid, outcome)
    assert second.settled is False

    with sessions() as session:
        reservation = session.execute(select(CostReservation)).scalar_one()
        assert reservation.settled_cost_micro_units == 3
    # 3 spent + 7 still held against the rest of the batch = 10 of the hold.
    assert service.remaining_budget_micro_units() == 90


def test_a_crash_between_operations_leaves_spent_money_spent(sessions) -> None:
    """Crash mid-batch: what ran is settled, what did not is returned.

    Three of twenty operations closed, then the worker died — no more
    heartbeats, the lease lapses, and the sweeper finds nothing open in
    C1's ledger. It must return the RESIDUAL hold only: crediting the
    three completed fetches back would make a crash cheaper than a
    success, which is the direction a budget must never be wrong in.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=20, estimated_requests=20)
    )

    _accrue_operations(service, grant.authorization_id, count=3, each=1)

    with sessions() as session:
        session.execute(
            text(
                "UPDATE cost_reservations SET lease_expires_at = now() - interval '1 hour'"
            )
        )
        session.commit()
    with sessions() as session:
        released, skipped = sweep_expired_reservations(session)

    assert (released, skipped) == (1, 0)
    with sessions() as session:
        reservation = session.execute(select(CostReservation)).scalar_one()
        assert reservation.state is ReservationState.RELEASED
        # The accrued spend SURVIVES the release; only the hold went back.
        assert reservation.settled_cost_micro_units == 3
    assert service.remaining_budget_micro_units() == 97


def test_the_lease_is_extended_while_any_operation_is_still_open(sessions) -> None:
    """...and the sweeper does NOT close a batch grant mid-flight.

    The terminal close of a batch grant belongs to the sweeper precisely
    because it asks C1's ledger first. One open operation is enough to
    keep the whole grant — and the rest of its batch's hold — alive.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=20, estimated_requests=20)
    )
    _accrue_operations(service, grant.authorization_id, count=3, each=1)

    with sessions() as session:
        session.add(
            NetworkOperation(
                id=uuid.uuid4(),
                network_request_id=uuid.uuid4(),
                canonical_url_hash="hash",
                domain=CERTIFIED_DOMAIN,
                provider="proxy",
                transport=NetworkTransport.PROXY,
                authorization_id=grant.authorization_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.execute(
            text(
                "UPDATE cost_reservations SET lease_expires_at = now() - interval '1 hour'"
            )
        )
        session.commit()

    with sessions() as session:
        released, skipped = sweep_expired_reservations(session)

    assert (released, skipped) == (0, 1)
    with sessions() as session:
        reservation = session.execute(select(CostReservation)).scalar_one()
        assert reservation.state is ReservationState.RESERVED
    # 3 spent, 17 still held against the batch's remaining targets.
    assert service.remaining_budget_micro_units() == 80


def test_accruals_from_concurrent_operations_all_land(sessions) -> None:
    """Twenty operations closing AT ONCE under one grant settle to 20.

    The reservation row is locked `FOR UPDATE` per accrual, so concurrent
    closes serialize rather than lose each other's increments — the
    read-then-write race that a non-locking accumulator would have.
    """
    workspace_id = _seed(sessions, limit_cost_micro_units=100)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(
        _req(workspace_id, estimated_cost_micro_units=20, estimated_requests=20)
    )

    def accrue(_):
        service.settle_partial(
            grant.authorization_id, SettledCost(cost_micro_units=1, requests=1)
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(accrue, range(20)))

    with sessions() as session:
        reservation = session.execute(select(CostReservation)).scalar_one()
        assert reservation.settled_cost_micro_units == 20
        assert reservation.settled_requests == 20
    assert service.remaining_budget_micro_units() == 80


def test_a_grant_carries_the_decision_facts_it_was_issued_on(sessions) -> None:
    """EPA Phase C F3: the grant hands out WHAT it decided, not just its id.

    Without these, C1's `entitlement_version`/`budget_decision_version`/
    `breaker_decision` columns had no producer at all and stood NULL in
    production — the ledger recorded which grant an operation ran under,
    but nothing about the decision that grant made.
    """
    workspace_id = _seed(sessions)
    service = _service(sessions, workspace_id=workspace_id)
    grant = service.authorize(_req(workspace_id))

    assert grant.entitlement_version == "saas-v7"
    assert grant.breaker_decision == ProxyBreakerState.CLOSED.value
    assert grant.budget_decision_version.startswith("cb1:")

    # A dedupe replay hands back the SAME decision facts, so two operations
    # opened under one grant can never claim different ones.
    replay = service.authorize(_req(workspace_id, dedupe_key="k"))
    again = service.authorize(_req(workspace_id, dedupe_key="k"))
    assert again.replayed is True
    assert again.entitlement_version == replay.entitlement_version
    assert again.breaker_decision == replay.breaker_decision
