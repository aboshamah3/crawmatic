"""Declared workspace scope for maintenance tasks (READY-007).

WHY THIS EXISTS
---------------
Every periodic maintenance task in this system is structurally
cross-tenant: it must first *find* work across every workspace, then do
that work *inside* one workspace at a time. Those are two different
database postures and the repo already has both primitives —
:func:`app_shared.database.get_system_session` (the sanctioned BYPASSRLS
role, id scans only) and
:func:`app_shared.database.set_workspace_context` (``SET LOCAL
app.workspace_id``, transaction-local, PgBouncer-safe).

What was missing is the *boundary between them*, and the missing
boundary is what broke ``strategy_stats_flush`` in production: it set
the workspace GUC once per workspace inside a **single** transaction and
then, after the loop, wrote every workspace's ``outbox_messages`` row
under whichever workspace happened to be last. ``SET LOCAL`` is
transaction-local, so the last writer wins for the whole transaction:
the first workspace's insert hit ``FORCE ROW LEVEL SECURITY``'s WITH
CHECK and raised ``new row violates row-level security policy for table
"outbox_messages"``, and every pending ORM UPDATE for an earlier
workspace silently matched zero rows on the way there.

So this module supplies two things:

1. :func:`workspace_context` — a context manager that makes "one
   workspace, one transaction" the *only* convenient way to write a
   tenant row. It opens the transaction, sets the GUC inside it, commits
   on success and rolls back on failure. A workspace's writes can
   therefore never leak into another workspace's scope, and no state
   survives on the pooled connection afterwards.
2. :func:`maintenance_task` + :func:`register_maintenance_tasks` — a
   declaration and a **runtime-enforced** registry. The decorator is not
   documentation: ``register_maintenance_tasks`` raises
   :class:`UndeclaredMaintenanceTaskError` for any Celery task handed to
   it without a declared scope, and
   ``tests/integration/test_maintenance_scoping.py`` feeds it every task
   in ``apps/workers`` so a new undeclared sweep fails CI rather than
   failing in production six months later.

The two scopes:

``fleet``
    The task's *entry point* spans workspaces. It may resolve ids
    through the sanctioned BYPASSRLS system role, but every row it then
    reads or writes must still happen inside a
    :func:`workspace_context` block.

``workspace``
    The task is handed its ``workspace_id`` by its caller and never
    scans across tenants. All of its work belongs in one
    :func:`workspace_context` block.

Scraping-free and Celery-free (Constitution I/V,
``tests/unit/test_import_boundaries.py``): this module imports only
stdlib + SQLAlchemy + ``app_shared.database``, so both the workers and
the scheduler can declare against it without dragging a broker import
into either closure.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app_shared.database import set_workspace_context

__all__ = [
    "MaintenanceScope",
    "UndeclaredMaintenanceTaskError",
    "WorkspaceContextError",
    "maintenance_scope_of",
    "maintenance_task",
    "register_maintenance_tasks",
    "task_identity",
    "workspace_context",
]

#: Attribute the decorator stamps onto the task. Read by
#: :func:`maintenance_scope_of` (and by nothing else — never poke at it
#: directly, the lookup has to unwrap Celery's Task wrapper).
_SCOPE_ATTR = "__maintenance_scope__"

T = TypeVar("T")


class MaintenanceScope(str, Enum):
    """How far a maintenance task's *entry point* reaches.

    ``str``-valued so ``@maintenance_task(scope="fleet")`` and
    ``@maintenance_task(scope=MaintenanceScope.FLEET)`` are the same
    declaration, and so a scope round-trips through logs/JSON unchanged.
    """

    #: Entry point spans every workspace: id resolution may use the
    #: sanctioned BYPASSRLS system role, row work may not.
    FLEET = "fleet"

    #: Entry point is handed one ``workspace_id`` and stays in it.
    WORKSPACE = "workspace"


class UndeclaredMaintenanceTaskError(RuntimeError):
    """A Celery task reached the registry without a declared scope.

    Raised by :func:`register_maintenance_tasks`. The message names every
    offending task so a developer who adds a sweep is told exactly what
    to add and where, rather than being told "something is undeclared".
    """


class WorkspaceContextError(RuntimeError):
    """:func:`workspace_context` was entered on a session that is already
    inside a transaction.

    This is the production bug expressed as a type. ``SET LOCAL
    app.workspace_id`` is scoped to the *transaction*, so nesting one
    workspace's scope inside another's would silently re-point every
    subsequent write in the outer transaction — exactly the
    "last workspace wins" failure that made ``strategy_stats_flush``
    raise an RLS violation for the first workspace it had already
    processed. Refusing to enter is the only safe answer: the caller must
    close (commit or roll back) the current transaction before scoping to
    a different workspace.
    """


def maintenance_task(
    *, scope: MaintenanceScope | str
) -> Callable[[T], T]:
    """Declare ``scope`` on a maintenance task. Apply ABOVE ``@app.task``.

    ::

        @maintenance_task(scope=MaintenanceScope.FLEET)
        @app.task(name=STRATEGY_STATS_FLUSH)
        def flush_stats() -> None:
            ...

    Applied outermost, the decorated object is Celery's ``Task``
    instance, which is what the registry and every caller of
    ``flush_stats`` actually hold — so the declaration travels with the
    thing that gets registered, not with an inner function nobody can
    reach. The scope is also stamped onto the underlying callable
    (``__wrapped__`` / ``run``) so :func:`maintenance_scope_of` finds it
    from either side.

    Returns the task unchanged apart from the stamped attribute — this
    never wraps, never changes the call signature, and never adds a call
    frame to a hot path.
    """
    resolved = MaintenanceScope(scope)

    def decorate(task: T) -> T:
        setattr(task, _SCOPE_ATTR, resolved)
        # Celery's Task instance and the plain function it wraps are two
        # different objects; stamp both so a caller holding either one
        # sees the same declaration.
        for attr in ("__wrapped__", "run"):
            inner = getattr(task, attr, None)
            if inner is not None and inner is not task:
                try:
                    setattr(inner, _SCOPE_ATTR, resolved)
                except (AttributeError, TypeError):  # pragma: no cover - builtins
                    pass
        return task

    return decorate


def maintenance_scope_of(task: Any) -> MaintenanceScope | None:
    """Return ``task``'s declared scope, or ``None`` if it has none.

    Looks at the object itself first, then at Celery's two indirections
    (``__wrapped__``, ``run``), so it answers the same way whether the
    caller holds the ``Task`` or the raw function.
    """
    for candidate in (task, getattr(task, "__wrapped__", None), getattr(task, "run", None)):
        if candidate is None:
            continue
        scope = getattr(candidate, _SCOPE_ATTR, None)
        if scope is not None:
            return MaintenanceScope(scope)
    return None


def task_identity(task: Any) -> str:
    """A human-locatable name for ``task`` — used only in error messages."""
    name = getattr(task, "name", None)
    if isinstance(name, str) and name:
        return name
    module = getattr(task, "__module__", "<unknown module>")
    qualname = getattr(task, "__qualname__", None) or getattr(task, "__name__", repr(task))
    return f"{module}.{qualname}"


def register_maintenance_tasks(tasks: Iterable[Any]) -> dict[str, MaintenanceScope]:
    """Validate that every task in ``tasks`` declares a scope.

    Returns ``{task identity: scope}`` for the whole batch when they all
    declare one.

    Raises:
        UndeclaredMaintenanceTaskError: if any task has no
            ``@maintenance_task(scope=...)`` declaration. Every offender
            is named in one message — a developer adding three sweeps at
            once should not have to run the check three times.

    This is the enforcement the decorator alone cannot provide. Import a
    module's tasks, hand them here, and an undeclared sweep is a failure
    at import/collection time rather than an RLS violation (or, worse, a
    silent zero-row sweep) in production.
    """
    registered: dict[str, MaintenanceScope] = {}
    undeclared: list[str] = []

    for task in tasks:
        identity = task_identity(task)
        scope = maintenance_scope_of(task)
        if scope is None:
            undeclared.append(identity)
            continue
        registered[identity] = scope

    if undeclared:
        raise UndeclaredMaintenanceTaskError(
            "Celery task(s) touch tenant tables without a declared workspace "
            "scope: "
            + ", ".join(sorted(undeclared))
            + ". Add @maintenance_task(scope=MaintenanceScope.FLEET) (the task's "
            "entry point spans workspaces; resolve ids on the system role and do "
            "every row write inside workspace_context) or "
            "@maintenance_task(scope=MaintenanceScope.WORKSPACE) (the task is "
            "handed one workspace_id) ABOVE the @app.task decorator. See "
            "app_shared.maintenance.scoping."
        )

    return registered


@contextmanager
def workspace_context(session: Session, workspace_id: uuid.UUID | str) -> Iterator[Session]:
    """Run a block in ONE transaction scoped to ``workspace_id``.

    ::

        for ws in fleet_scan():                 # ids only, system role
            with workspace_context(session, ws):
                ...                             # every row read/write
                write_outbox_message(session, workspace_id=ws, ...)

    The transaction is opened here, ``app.workspace_id`` is set inside it
    with :func:`app_shared.database.set_workspace_context` (``set_config
    (..., true)`` — ``SET LOCAL`` semantics, a bound parameter, never
    interpolated SQL), and the block runs with RLS resolving to exactly
    that workspace. Leaving the block normally commits; raising rolls
    back. Either way the transaction ends, so:

    * the GUC dies with the transaction and cannot ride a pooled
      connection back to an unrelated caller (PgBouncer transaction
      pooling — see :mod:`app_shared.database`'s module docstring);
    * one workspace's failure aborts only that workspace's work, leaving
      the sweep free to continue with the next one;
    * pending ORM state can never be flushed under a *different*
      workspace's GUC, because there is no later GUC in this
      transaction.

    Raises:
        WorkspaceContextError: if ``session`` is already inside a
            transaction. Nesting is exactly the bug this exists to
            prevent — see :class:`WorkspaceContextError`.
    """
    if session.in_transaction():
        raise WorkspaceContextError(
            "workspace_context requires a session with no open transaction "
            f"(scoping to workspace_id={workspace_id}). `SET LOCAL "
            "app.workspace_id` is transaction-local, so entering here would "
            "re-scope the transaction already in progress and silently move "
            "its pending writes into this workspace. Commit or roll back the "
            "current transaction first — one workspace, one transaction."
        )

    with session.begin():
        set_workspace_context(session, workspace_id)
        yield session
