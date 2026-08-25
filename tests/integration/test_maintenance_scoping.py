"""Maintenance-task workspace scoping under real RLS (READY-007).

## The production failure this pins

``maintenance.strategy_stats_flush`` ran every cycle and raised::

    psycopg.errors.InsufficientPrivilege: new row violates row-level
    security policy for table "outbox_messages"

`app.workers.tasks_strategy.flush_stats` is a fleet sweep: it scans every
workspace owning a strategy profile, flushes each workspace's dirty
profiles, collects the promotion/rediscovery transitions, and writes one
outbox row per transition. All of that used to happen inside **one**
transaction with ``set_workspace_context`` called once per workspace
*inside* the loop. ``SET LOCAL app.workspace_id`` is transaction-local,
so the last workspace in the loop owned the GUC for the whole
transaction — and the outbox inserts, which ran after the loop, were
written under that one workspace's scope. Every transition belonging to
any earlier workspace hit ``FORCE ROW LEVEL SECURITY``'s WITH CHECK and
raised. (The same re-scoping also flushed earlier workspaces' pending
ORM UPDATEs under the wrong GUC, where RLS matched them to zero rows and
lost them silently — no error at all, which is the worse half.)

## Why these tests run against a real, confined role

A superuser or table-owner connection bypasses RLS unconditionally, so
this bug is *invisible* to a test that reaches Postgres the convenient
way — the same blind spot that hid the 2026-08-21 finalization outage
for 6.5 h. So this file reuses the shipped harness verbatim: the
``provisioned`` fixture from ``test_rls_cross_workspace.py`` migrates a
throwaway database, runs the real ``migrate.provision_roles`` deploy
step, and asserts ``crawmatic_app`` is NOSUPERUSER/NOBYPASSRLS before any
claim below is made. The sweep is then driven with its ordinary engine
bound to that confined role and its system engine bound to the BYPASSRLS
``crawmatic_auth`` role — production's exact wiring.

## Running it

    docker run -d --name cm-rls-test \\
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55446:5432 postgres:16

    RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55446/crawmatic \\
    CM_TEST_REDIS_URL=redis://127.0.0.1:56379/9 \\
      uv run pytest tests/integration/test_maintenance_scoping.py -v

Skips loudly (never silently passes) without a reachable owner URL; the
two flush tests additionally skip without a reachable Redis, since the
stats buffer they drain is a real Redis structure and faking it would
stop proving anything about the shipped task.

## Why the tasks run in a subprocess

``apps/api/app`` and ``apps/workers/app`` are both named ``app`` and the
API one wins on the installed path, so ``app.workers.*`` is importable
only with ``apps/workers`` prepended to ``sys.path`` in a process that
has not already resolved ``app``. Every worker-task check in this repo
therefore drives the task from a subprocess script
(``test_finalize_under_rls.py``), and these do the same.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# The harness is reused, not re-implemented: `provisioned` migrates the
# throwaway database, runs the real `provision_roles` deploy step, and
# skips loudly (with instructions) when no owner URL is reachable.
from integration.test_rls_cross_workspace import provisioned  # noqa: F401 - pytest fixture

from app_shared.maintenance.scoping import (
    MaintenanceScope,
    UndeclaredMaintenanceTaskError,
    WorkspaceContextError,
    maintenance_scope_of,
    maintenance_task,
    register_maintenance_tasks,
    workspace_context,
)
from app_shared.models.outbox import OutboxMessage
from app_shared.task_names import CREATE_WEBHOOK_EVENT

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The outbox ``task_name`` a strategy transition is published under.
#:
#: DEVIATION from the B7 brief, which specified
#: ``task_name="strategy.publish_transition"``. No such Celery task
#: exists anywhere in this repo — no name constant, no consumer, no
#: queue route. Publishing transitions under it would hand the outbox
#: dispatcher a ``send_task`` for a task no worker is registered for,
#: replacing a live RLS failure with silently undelivered strategy
#: webhooks. The shipped seam (`tasks_strategy._outbox_strategy_
#: transition` -> `tasks_webhooks.create_webhook_event`) is used instead,
#: unchanged; what READY-007 actually fixes is the *scope* the row is
#: inserted under, not the row's task name.
STRATEGY_TRANSITION_TASK_NAME = CREATE_WEBHOOK_EVENT

#: Settings are validated at import time of the worker task modules, so
#: the subprocess needs a complete-looking environment. Only the database
#: URLs and REDIS_URL matter to what is being proven here.
_BASE_WORKER_ENV = {
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",  # noqa: S106 - throwaway test environment
    "JWT_SECRET": "test-jwt-secret",  # noqa: S106 - throwaway test environment
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

#: Wires the worker's two engines exactly the way production wires them —
#: ordinary engine on the RLS-confined role, system engine on the
#: BYPASSRLS role — then runs the SHIPPED task.
_FLUSH_SCRIPT = """
import os
import sys

sys.path.insert(0, "apps/workers")

from sqlalchemy import create_engine

import app_shared.database as db


def _make(url):
    return create_engine(url, pool_pre_ping=True, connect_args={"prepare_threshold": None})


db._engine = _make(os.environ["CM_B7_ORDINARY_URL"])
db._sessionmaker = None
db._system_engine = _make(os.environ["CM_B7_SCAN_URL"])
db._system_sessionmaker = None

from app.workers.tasks_strategy import flush_stats

flush_stats()

print("OK")
"""

#: Imports every Celery task module under `apps/workers` and hands every
#: task it finds to the registry. An undeclared sweep raises
#: `UndeclaredMaintenanceTaskError` here, in CI, instead of writing a row
#: under the wrong workspace in production. Emits one JSON line so the
#: parent can assert on the actual declarations, not just on exit code.
_REGISTRY_SCRIPT = """
import importlib
import json
import pkgutil
import sys

sys.path.insert(0, "apps/workers")

from celery import Task

from app_shared.maintenance.scoping import register_maintenance_tasks

import app.workers as workers_pkg

discovered = []
for info in pkgutil.iter_modules(workers_pkg.__path__):
    if not info.name.startswith("tasks_"):
        continue
    module = importlib.import_module("app.workers." + info.name)
    for attr in vars(module).values():
        if not isinstance(attr, Task):
            continue
        # `@app.task` leaves a `celery.local.Proxy` in the module
        # namespace, so the Proxy's own `__module__` is "celery.local" —
        # the defining module is only visible on the underlying callable.
        run = getattr(attr, "run", None)
        if getattr(run, "__module__", None) == module.__name__:
            discovered.append(attr)

registered = register_maintenance_tasks(discovered)
print(json.dumps({name: scope.value for name, scope in registered.items()}))
"""


def _redis_url() -> str:
    return os.environ.get("CM_TEST_REDIS_URL", "redis://127.0.0.1:56379/9")


def _redis_or_skip():
    redis_module = pytest.importorskip("redis")
    client = redis_module.Redis.from_url(_redis_url())
    try:
        client.ping()
    except Exception:  # noqa: BLE001 - any connection failure is a skip
        pytest.skip(
            f"No reachable Redis at {_redis_url()} for the strategy stats buffer "
            "(set CM_TEST_REDIS_URL)"
        )
    return client


@pytest.fixture()
def owner_engine(provisioned: dict[str, str]) -> Iterator[Engine]:
    """The owner/superuser connection — used ONLY to seed and to assert.

    Deliberately never used to *exercise* anything: an owner bypasses RLS,
    so a claim made through it would be a claim about nothing.
    """
    engine = create_engine(provisioned["admin_url"])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def app_engine(provisioned: dict[str, str]) -> Iterator[Engine]:
    """`crawmatic_app` — NOSUPERUSER, NOBYPASSRLS, asserted so by
    `provision_roles.verify()` inside `provisioned`."""
    engine = create_engine(provisioned["app_url"])
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_workspace_with_dirty_profile(
    owner: Engine, redis_client, label: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """One workspace + competitor + LEARNING strategy profile, with three
    qualifying successes across three distinct URLs buffered in Redis.

    Three qualifying successes over three distinct URLs is exactly the
    promotion bar (`STRATEGY_PROMOTION_MIN_SUCCESSES` = 3,
    `STRATEGY_PROMOTION_MIN_DISTINCT_URLS` = 3), so `flush_profile` will
    promote the profile to ACTIVE and surface one genuine transition —
    which is what makes the flush write an `outbox_messages` row at all.

    Seeded through the owner connection as plain INSERTs so the seed
    itself is not a claim about what the confined role can do.
    """
    from app_shared.enums import MethodType
    from app_shared.strategy import stats_buffer

    workspace_id = uuid.uuid4()
    competitor_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    domain = f"{label}-{workspace_id.hex[:8]}.example.com"

    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', :now, :now)"
            ),
            {
                "id": str(workspace_id),
                "name": f"READY-007 {label}",
                "slug": f"ready007-{label}-{workspace_id.hex[:8]}",
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO competitors (id, workspace_id, name, domain, status, "
                " legal_status, robots_policy, created_at, updated_at) "
                "VALUES (:id, :ws, :name, :domain, 'ACTIVE', 'ALLOWED', 'RESPECT', "
                " :now, :now)"
            ),
            {
                "id": str(competitor_id),
                "ws": str(workspace_id),
                "name": f"Competitor {label}",
                "domain": domain,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO domain_strategy_profiles "
                "(id, workspace_id, competitor_id, domain, url_pattern, "
                " url_pattern_version, status, confirmed_success_count, "
                " recent_failure_count, created_at, updated_at) "
                "VALUES (:id, :ws, :comp, :domain, :pattern, 1, 'LEARNING', 0, 0, "
                " :now, :now)"
            ),
            {
                "id": str(profile_id),
                "ws": str(workspace_id),
                "comp": str(competitor_id),
                "domain": domain,
                "pattern": f"https://{domain}/p/*",
                "now": now,
            },
        )

    for index in range(3):
        stats_buffer.record_attempt(
            redis_client,
            workspace_id=workspace_id,
            profile_id=profile_id,
            method_type=MethodType.ACCESS,
            method_name="DIRECT_HTTP",
            success=True,
            response_time_ms=120,
            confidence=0.95,
            url=f"https://{domain}/p/item-{index}",
            qualifying=True,
            ttl_seconds=3600,
        )

    return workspace_id, profile_id


@pytest.fixture()
def dirty_strategy_stats(
    owner_engine: Engine,
) -> Iterator[list[tuple[uuid.UUID, uuid.UUID]]]:
    """TWO workspaces, each with one dirty, promotable strategy profile.

    Two is the whole point: with a single workspace the buggy
    "one transaction, GUC re-set per workspace, outbox written after the
    loop" shape happens to work, because the last workspace *is* the only
    workspace. The RLS violation only appears once a transition belonging
    to an earlier workspace has to be inserted under a later workspace's
    scope.
    """
    redis_client = _redis_or_skip()

    seeded = [
        _seed_workspace_with_dirty_profile(owner_engine, redis_client, "alpha"),
        _seed_workspace_with_dirty_profile(owner_engine, redis_client, "beta"),
    ]
    try:
        yield seeded
    finally:
        from app_shared.strategy import stats_buffer

        for workspace_id, profile_id in seeded:
            try:
                redis_client.delete(stats_buffer.dirty_key(workspace_id))
                for key in redis_client.scan_iter(match=f"strat*{profile_id}*"):
                    redis_client.delete(key)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            _purge(owner_engine, workspace_id)


def _purge(owner: Engine, workspace_id: uuid.UUID) -> None:
    with owner.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM strategy_attempt_stats WHERE domain_strategy_profile_id IN "
                "(SELECT id FROM domain_strategy_profiles WHERE workspace_id = :ws)"
            ),
            {"ws": str(workspace_id)},
        )
        for table in (
            "outbox_messages",
            "domain_strategy_methods",
            "domain_strategy_profiles",
            "competitors",
            "workspaces",
        ):
            column = "id" if table == "workspaces" else "workspace_id"
            conn.execute(
                text(f"DELETE FROM {table} WHERE {column} = :ws"),  # noqa: S608 - fixed names
                {"ws": str(workspace_id)},
            )


def _run_script(
    script: str, *, ordinary_url: str, scan_url: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            **_BASE_WORKER_ENV,
            "REDIS_URL": _redis_url(),
            "DATABASE_URL": ordinary_url,
            "SYSTEM_DATABASE_URL": scan_url,
            "AUTH_DATABASE_URL": scan_url,
            "CM_B7_ORDINARY_URL": ordinary_url,
            "CM_B7_SCAN_URL": scan_url,
        },
    )


def _transition_rows(owner: Engine, workspace_id: uuid.UUID) -> list[tuple[str, str]]:
    with owner.connect() as conn:
        return [
            (row.task_name, row.dedup_key)
            for row in conn.execute(
                text(
                    "SELECT task_name, dedup_key FROM outbox_messages "
                    "WHERE workspace_id = :ws AND task_name = :task "
                    "ORDER BY created_at"
                ),
                {"ws": str(workspace_id), "task": STRATEGY_TRANSITION_TASK_NAME},
            ).all()
        ]


# --- 1. the production failure ------------------------------------------


def test_strategy_flush_outbox_insert_succeeds_under_rls(
    owner_engine: Engine,
    provisioned: dict[str, str],
    dirty_strategy_stats: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """The shipped fleet sweep must complete and enqueue one transition
    message per dirty workspace, under the confined (NOBYPASSRLS) role.

    Before READY-007 this failed with
    ``new row violates row-level security policy for table
    "outbox_messages"`` — the first workspace's outbox insert written
    under the last workspace's ``SET LOCAL app.workspace_id``.
    """
    result = _run_script(
        _FLUSH_SCRIPT,
        ordinary_url=provisioned["app_url"],
        scan_url=provisioned["auth_url"],
    )

    assert result.returncode == 0, (
        "strategy_stats_flush raised under the production role split\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr[-4000:]!r}"
    )
    assert result.stdout.strip().endswith("OK")

    total = 0
    for workspace_id, _profile_id in dirty_strategy_stats:
        rows = _transition_rows(owner_engine, workspace_id)
        assert len(rows) == 1, (
            f"workspace {workspace_id} should have exactly one transition "
            f"message, got {rows!r}"
        )
        assert rows[0][1], "every transition message must carry a dedup_key"
        total += len(rows)

    assert total == len(dirty_strategy_stats)


def test_strategy_flush_promotes_every_workspace_not_just_the_last(
    owner_engine: Engine,
    provisioned: dict[str, str],
    dirty_strategy_stats: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """The silent half of the same bug.

    Re-scoping mid-transaction did not only break the outbox insert: an
    earlier workspace's pending ORM UPDATEs were flushed under a later
    workspace's GUC, where RLS's USING clause matched them to zero rows.
    No exception, no log line, no promotion — so the status change is
    asserted directly, for **every** seeded workspace.
    """
    result = _run_script(
        _FLUSH_SCRIPT,
        ordinary_url=provisioned["app_url"],
        scan_url=provisioned["auth_url"],
    )
    assert result.returncode == 0, f"stderr={result.stderr[-4000:]!r}"

    with owner_engine.connect() as conn:
        for workspace_id, profile_id in dirty_strategy_stats:
            row = conn.execute(
                text(
                    "SELECT status, preferred_access_method FROM "
                    "domain_strategy_profiles WHERE id = :id"
                ),
                {"id": str(profile_id)},
            ).one()
            assert row.status == "ACTIVE", (
                f"profile {profile_id} (workspace {workspace_id}) was not promoted"
            )
            assert row.preferred_access_method == "DIRECT_HTTP"


# --- 2. RLS rejects an unscoped outbox insert ----------------------------


def test_outbox_insert_without_workspace_context_is_rejected(
    app_engine: Engine, owner_engine: Engine
) -> None:
    """With no ``app.workspace_id`` set, the confined role cannot insert an
    outbox row. The rejection IS the correct behavior — it is the backstop
    that turned READY-007 into a loud failure instead of a cross-tenant
    leak, and it must keep working after the fix.
    """
    from sqlalchemy.orm import Session as OrmSession

    workspace_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', :now, :now)"
            ),
            {
                "id": str(workspace_id),
                "name": "READY-007 unscoped",
                "slug": f"ready007-unscoped-{workspace_id.hex[:8]}",
                "now": now,
            },
        )

    try:
        with OrmSession(bind=app_engine) as session:
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 - RLS violation is the point
                session.add(
                    OutboxMessage(
                        id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        task_name=STRATEGY_TRANSITION_TASK_NAME,
                        queue="webhook_events",
                        payload={},
                        dedup_key=f"unscoped-{uuid.uuid4().hex}",
                        status="PENDING",
                        attempts=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
            session.rollback()

        assert "row-level security" in str(excinfo.value).lower()
    finally:
        _purge(owner_engine, workspace_id)


def test_workspace_context_permits_the_same_insert(
    app_engine: Engine, owner_engine: Engine
) -> None:
    """The positive control for the test above: the identical insert
    succeeds through the identical confined role once it runs inside
    ``workspace_context`` — proving the rejection above is about missing
    scope, not about a role that simply cannot write."""
    from sqlalchemy.orm import Session as OrmSession

    from app_shared.outbox import write_outbox_message

    workspace_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', :now, :now)"
            ),
            {
                "id": str(workspace_id),
                "name": "READY-007 scoped",
                "slug": f"ready007-scoped-{workspace_id.hex[:8]}",
                "now": now,
            },
        )

    try:
        with OrmSession(bind=app_engine) as session:
            with workspace_context(session, workspace_id):
                write_outbox_message(
                    session,
                    workspace_id=workspace_id,
                    task_name=STRATEGY_TRANSITION_TASK_NAME,
                    queue="webhook_events",
                    kwargs={"workspace_id": str(workspace_id)},
                    dedup_key=f"scoped-{uuid.uuid4().hex}",
                )

        rows = _transition_rows(owner_engine, workspace_id)
        assert len(rows) == 1
    finally:
        _purge(owner_engine, workspace_id)


def test_workspace_context_ends_its_transaction_and_leaves_no_guc(
    app_engine: Engine, owner_engine: Engine
) -> None:
    """Pool safety: the GUC must not survive the block.

    Under PgBouncer transaction pooling the connection goes back to the
    pool between transactions, so a workspace scope that outlived its
    transaction would be handed to an unrelated caller. ``SET LOCAL``
    semantics are what prevent that, and this asserts them on the shipped
    helper rather than trusting the flag.
    """
    from sqlalchemy.orm import Session as OrmSession

    workspace_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', :now, :now)"
            ),
            {
                "id": str(workspace_id),
                "name": "READY-007 guc",
                "slug": f"ready007-guc-{workspace_id.hex[:8]}",
                "now": now,
            },
        )

    try:
        with OrmSession(bind=app_engine) as session:
            with workspace_context(session, workspace_id) as scoped:
                inside = scoped.execute(
                    text("SELECT current_setting('app.workspace_id', true)")
                ).scalar()
                assert inside == str(workspace_id)

            assert not session.in_transaction(), (
                "workspace_context must close its own transaction so the GUC "
                "cannot ride a pooled connection to the next caller"
            )
            after = session.execute(
                text("SELECT NULLIF(current_setting('app.workspace_id', true), '')")
            ).scalar()
            assert after is None
            session.rollback()
    finally:
        _purge(owner_engine, workspace_id)


def test_workspace_context_refuses_to_nest(app_engine: Engine) -> None:
    """Entering a second workspace's scope inside an open transaction is
    the production bug itself; the helper must refuse rather than
    silently re-point the transaction."""
    from sqlalchemy.orm import Session as OrmSession

    with OrmSession(bind=app_engine) as session:
        with workspace_context(session, uuid.uuid4()):
            with pytest.raises(WorkspaceContextError):
                with workspace_context(session, uuid.uuid4()):
                    pass  # pragma: no cover - the enter must raise
        session.rollback()


# --- 3. the registry is enforcement, not documentation -------------------


def _task_without_scope_declaration() -> None:
    """Stands in for a newly added maintenance sweep nobody declared."""


@maintenance_task(scope=MaintenanceScope.FLEET)
def _task_with_scope_declaration() -> None:
    """Stands in for a correctly declared fleet sweep."""


def test_undeclared_maintenance_task_is_rejected_by_registry() -> None:
    with pytest.raises(UndeclaredMaintenanceTaskError) as excinfo:
        register_maintenance_tasks([_task_without_scope_declaration])

    # The error has to name the offender, or a developer adding a sweep
    # learns only that "something" is undeclared.
    assert "_task_without_scope_declaration" in str(excinfo.value)


def test_declared_maintenance_task_is_accepted_by_registry() -> None:
    registered = register_maintenance_tasks([_task_with_scope_declaration])

    assert list(registered.values()) == [MaintenanceScope.FLEET]
    assert maintenance_scope_of(_task_with_scope_declaration) is MaintenanceScope.FLEET


def test_scope_declaration_accepts_plain_strings() -> None:
    """``scope="fleet"`` and ``scope=MaintenanceScope.FLEET`` are the same
    declaration — call sites should not have to import the enum."""

    @maintenance_task(scope="workspace")
    def _handed_a_workspace_id() -> None: ...

    assert maintenance_scope_of(_handed_a_workspace_id) is MaintenanceScope.WORKSPACE

    with pytest.raises(ValueError):
        maintenance_task(scope="whole-fleet-ish")


def test_every_worker_celery_task_declares_a_scope() -> None:
    """The CI-runnable enforcement: import every ``apps/workers`` task
    module and put every task it defines through the registry.

    Adding a Celery task that touches tenant tables without declaring a
    scope fails HERE — not in production, six months later, as an RLS
    violation or (worse) a silent zero-row sweep.

    ``apps/scheduler`` defines no Celery tasks of its own (it is a beat
    process that enqueues by name), so there is nothing there to declare;
    if that ever changes, this test's discovery walk is the place to
    extend.
    """
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRY_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            **_BASE_WORKER_ENV,
            "REDIS_URL": _redis_url(),
            "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        },
    )

    assert result.returncode == 0, (
        "a Celery task in apps/workers has no @maintenance_task(scope=...) "
        f"declaration\nstdout={result.stdout!r}\nstderr={result.stderr[-4000:]!r}"
    )

    declared = json.loads(result.stdout.strip().splitlines()[-1])

    # Spot-check the two shapes rather than pinning the whole map (which
    # would turn every new task into a two-place edit): the fleet sweep
    # this task fixes, and a task handed its workspace_id by its caller.
    assert declared["maintenance.strategy_stats_flush"] == "fleet"
    assert declared["webhook_events.create_webhook_event"] == "workspace"
    assert len(declared) >= 15, f"discovery walk found too few tasks: {declared!r}"
