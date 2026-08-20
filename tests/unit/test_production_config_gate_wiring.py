"""Every service entrypoint runs the §L1 production-config gate.

`app_shared.config_validation.assert_production_safe` shipped wired into
exactly one of three long-running services — the API
(`apps/api/app/main.py`) — so a production deploy carrying
`.env.example`-shaped secrets, `PGBOUNCER_AUTH_TYPE=trust`, or the DB
bootstrap/owner role as `DATABASE_URL` was refused at the API and
accepted everywhere else. The scheduler drives fleet-wide maintenance and
the worker executes it, both on the BYPASSRLS system session, so a gate
that only guards the read/write API guards the least of the three.

These tests pin the two new wirings *and their failure behaviour*:

* the scheduler calls it as the first statement of `main()`, and a
  `ProductionConfigError` propagates out of `main()` — the loop never
  starts;
* the worker calls it from `worker_init` (once, in the long-lived parent,
  before the pool forks), and a `ProductionConfigError` becomes a
  `SystemExit` so Celery's `Signal.send` — which swallows every
  `Exception` a receiver raises and returns it as that receiver's
  response — cannot let an unsafe worker boot anyway.

The last point is the whole reason the worker receiver is not a bare
`assert_production_safe()` call, and
`test_worker_gate_survives_celerys_exception_swallowing_dispatch` drives
the *real* `worker_init.send` to prove it rather than trusting the
reading of Celery's source.

Loaded in fresh subprocesses (mirrors `test_scheduler_outbox_wiring.py` /
`test_celery_delivery_reliability.py`): `apps/api`, `apps/workers` and
`apps/scheduler` each ship their own top-level `app` package, and
`celery_app.py` calls `get_settings()` at module scope.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: Local-dev-shaped config, exactly `.env.example`'s placeholders — what
#: `assert_production_safe` must refuse *once* `ENVIRONMENT=production`.
_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _run(body: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **_ENV}
    # Never let a stray production marker from the developer's own
    # environment leak in and flip the no-op tests into raising ones.
    env.pop("ENVIRONMENT", None)
    env.pop("RAILWAY_ENVIRONMENT_NAME", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


_SCHEDULER_SETUP = """
import sys
sys.path.insert(0, "apps/scheduler")

from app.scheduler import scheduler_app
"""

_WORKER_SETUP = """
import sys
sys.path.insert(0, "apps/workers")

from app.workers import celery_app as celery_app_module
"""


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------


def test_scheduler_main_calls_the_production_gate() -> None:
    """`main()` must actually invoke it — not merely import it."""
    _assert_ok(
        _run(
            _SCHEDULER_SETUP
            + """
calls = []
scheduler_app.assert_production_safe = lambda *a, **kw: calls.append(1)

# Stop `main()` the moment the gate has been passed: everything after it
# is the real loop, which we are not exercising here.
class _Stop(Exception):
    pass

def _boom(*a, **kw):
    raise _Stop()

scheduler_app.signal.signal = _boom

try:
    scheduler_app.main()
except _Stop:
    pass

assert calls == [1], calls
print("OK")
"""
        )
    )


def test_scheduler_gate_runs_before_anything_else_in_main() -> None:
    """Fail *fast*: before signal handlers, before `get_settings()`, and
    long before the first cadence/health pass touches the database."""
    _assert_ok(
        _run(
            _SCHEDULER_SETUP
            + """
import inspect

body = [
    line.strip()
    for line in inspect.getsource(scheduler_app.main).splitlines()
    if line.strip() and not line.strip().startswith("#")
]
# [0] is the `def main() -> None:` line itself.
assert body[1] == "assert_production_safe()", body[:4]
print("OK")
"""
        )
    )


def test_scheduler_refuses_to_start_when_the_gate_fails() -> None:
    """A failing check must prevent startup — `main()` propagates, the
    tick loop is never entered."""
    _assert_ok(
        _run(
            _SCHEDULER_SETUP
            + """
from app_shared.config_validation import ProductionConfigError

entered_loop = []
scheduler_app.time.sleep = lambda *a, **kw: entered_loop.append(1)

try:
    scheduler_app.main()
except ProductionConfigError as exc:
    message = str(exc)
else:
    raise AssertionError("main() started under unsafe production config")

assert "Refusing to start in production mode" in message, message
assert entered_loop == [], entered_loop
print("OK")
""",
            extra_env={"ENVIRONMENT": "production"},
        )
    )


def test_scheduler_gate_is_a_noop_outside_production() -> None:
    """The local/dev/CI path: the very same placeholder config must boot."""
    _assert_ok(
        _run(
            _SCHEDULER_SETUP
            + """
from app_shared.config_validation import assert_production_safe, is_production

assert is_production() is False
assert_production_safe()  # must not raise on `.env.example` values
print("OK")
"""
        )
    )


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


def test_worker_gate_is_wired_to_worker_init_not_module_import() -> None:
    """Import must stay side-effect-free: every `app.workers.tasks_*`
    module imports `celery_app` just to get the `app` object, and so does
    this test suite. The gate belongs on the worker lifecycle instead."""
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
from celery.signals import worker_init

# Importing the module under production-shaped-but-unsafe config did not
# raise (we got here), and the receiver is registered on worker_init.
assert celery_app_module.app is not None
names = {getattr(r, "__name__", None) for r in worker_init._live_receivers(None)}
assert "_assert_production_safe_on_worker_start" in names, names
print("OK")
""",
            extra_env={"ENVIRONMENT": "production"},
        )
    )


def test_worker_gate_runs_before_the_memory_watchdog_receiver() -> None:
    """Receivers fire in connection order, so an unsafe deploy must die
    before it spawns the watchdog thread or forks a single child."""
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
from celery.signals import worker_init

assert celery_app_module.app is not None
names = [getattr(r, "__name__", None) for r in worker_init._live_receivers(None)]
gate = names.index("_assert_production_safe_on_worker_start")
watchdog = names.index("_start_memory_watchdog")
assert gate < watchdog, names
print("OK")
"""
        )
    )


def test_worker_gate_calls_assert_production_safe() -> None:
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
calls = []
celery_app_module.assert_production_safe = lambda *a, **kw: calls.append(1)

celery_app_module._assert_production_safe_on_worker_start()

assert calls == [1], calls
print("OK")
"""
        )
    )


def test_worker_receiver_turns_a_failing_check_into_systemexit() -> None:
    """`ProductionConfigError` is a `RuntimeError`; Celery's dispatcher
    swallows those. `SystemExit` is a `BaseException` and is not
    swallowed — that conversion IS the fail-fast."""
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
try:
    celery_app_module._assert_production_safe_on_worker_start()
except SystemExit as exc:
    assert exc.code == 1, exc.code
else:
    raise AssertionError("worker startup was not aborted under unsafe config")
print("OK")
""",
            extra_env={"ENVIRONMENT": "production"},
        )
    )


def test_worker_gate_survives_celerys_exception_swallowing_dispatch() -> None:
    """The regression guard for the whole design.

    Drives the REAL `worker_init.send(...)` — the call
    `celery.worker.worker.WorkController.__init__` makes — and asserts it
    does not return normally. Rewrite the receiver to `raise
    ProductionConfigError` and this test fails: `Signal.send` catches
    every `Exception` and hands it back as a *response*, so the worker
    would carry on booting with unsafe production configuration.
    """
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
from celery.signals import worker_init

try:
    worker_init.send(sender=None)
except SystemExit as exc:
    assert exc.code == 1, exc.code
else:
    raise AssertionError(
        "worker_init.send() returned normally under unsafe production config "
        "-- Celery swallowed the failure and the worker would have booted"
    )
print("OK")
""",
            extra_env={"ENVIRONMENT": "production"},
        )
    )


def test_worker_boots_normally_outside_production() -> None:
    """Local/dev/CI: the same placeholder config passes the whole
    `worker_init` chain, watchdog receiver included."""
    _assert_ok(
        _run(
            _WORKER_SETUP
            + """
from celery.signals import worker_init

worker_init.send(sender=None)  # must not raise or exit
print("OK")
"""
        )
    )
