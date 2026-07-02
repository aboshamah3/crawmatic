"""Engine-hygiene unit tests (FR-008, FR-009, FR-010; SC-005).

All DB-independent — no real Postgres connection is ever opened:

* (a) importing ``app_shared.database`` and ``app_shared.models`` in a
  fresh subprocess creates NO engine (module-level ``_engine is None``
  until ``get_engine()`` is called) — [analyze SC-005].
* (b) fork-disposal — ``dispose_engine()`` resets the module-level
  ``_engine``/``_sessionmaker`` singletons to ``None`` and calls
  ``.dispose()`` on the stubbed-in engine, WITHOUT opening a live DB
  connection; the Celery ``worker_process_init`` hook in
  ``apps/workers/app/workers/celery_app.py`` is wired to call it —
  [analyze G1].
* (c) pooler-safe config — the engine build path passes
  ``connect_args={"prepare_threshold": None}`` so server-side prepared
  statements are disabled under PgBouncer transaction pooling —
  [analyze G2].
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import app_shared.database as database


# ---------------------------------------------------------------------------
# (a) No eager engine at import time (FR-008, SC-005)
# ---------------------------------------------------------------------------

_NO_EAGER_ENGINE_CHECK = """
import sys

import app_shared.database as database
import app_shared.models  # noqa: F401

if database._engine is not None:
    print("ENGINE_CREATED_AT_IMPORT")
    sys.exit(1)

if database._sessionmaker is not None:
    print("SESSIONMAKER_CREATED_AT_IMPORT")
    sys.exit(1)

sys.exit(0)
"""


def test_importing_database_and_models_creates_no_engine() -> None:
    """A fresh subprocess importing app_shared.database/models creates 0 engines."""
    result = subprocess.run(
        [sys.executable, "-c", _NO_EAGER_ENGINE_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) Fork-disposal: dispose_engine() resets singletons + calls .dispose()
# ---------------------------------------------------------------------------


def test_dispose_engine_resets_singletons_and_disposes_stub_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force-create stub singletons (no real DB connect), then assert dispose_engine()
    resets them to None and calls .dispose() on the stubbed engine.
    """
    disposed = {"called": False}

    class _StubEngine:
        def dispose(self) -> None:
            disposed["called"] = True

    stub_engine = _StubEngine()
    stub_sessionmaker = SimpleNamespace()

    monkeypatch.setattr(database, "_engine", stub_engine, raising=False)
    monkeypatch.setattr(database, "_sessionmaker", stub_sessionmaker, raising=False)

    assert database._engine is stub_engine
    assert database._sessionmaker is stub_sessionmaker

    database.dispose_engine()

    assert disposed["called"] is True
    assert database._engine is None
    assert database._sessionmaker is None


def test_dispose_engine_is_a_noop_when_no_engine_was_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling dispose_engine() before any engine exists must not error or connect."""
    monkeypatch.setattr(database, "_engine", None, raising=False)
    monkeypatch.setattr(database, "_sessionmaker", None, raising=False)

    database.dispose_engine()  # must not raise

    assert database._engine is None
    assert database._sessionmaker is None


# Loaded in a fresh subprocess (not in-process import) for two reasons:
# (1) several app members ship their own top-level ``app`` package (e.g.
# ``apps/api/app``, ``apps/workers/app``), so a plain ``import
# app.workers.celery_app`` in the test process is ambiguous — whichever
# member happens to be first on sys.path wins; (2) celery_app.py calls
# get_settings() at module scope, which needs the required env vars —
# a subprocess lets us supply a clean, self-contained environment
# without polluting the shared get_settings() lru_cache used by other
# tests in this process.
_CELERY_HOOK_WIRING_CHECK = """
import sys

sys.path.insert(0, "apps/workers/app")
import workers.celery_app as celery_app_module

from app_shared.database import dispose_engine
from celery.signals import worker_process_init

if celery_app_module.dispose_engine is not dispose_engine:
    print("NOT_SAME_DISPOSE_ENGINE")
    sys.exit(1)

import inspect
source = inspect.getsource(celery_app_module._dispose_inherited_engine)
if "dispose_engine" not in source:
    print("HANDLER_DOES_NOT_CALL_DISPOSE_ENGINE")
    sys.exit(1)

import weakref

connected_names = []
for _id, ref in worker_process_init.receivers:
    receiver = ref() if isinstance(ref, weakref.ReferenceType) else ref
    if receiver is not None:
        connected_names.append(getattr(receiver, "__name__", ""))

if "_dispose_inherited_engine" not in connected_names:
    print("HANDLER_NOT_CONNECTED_TO_SIGNAL:" + ",".join(connected_names))
    sys.exit(1)

sys.exit(0)
"""

_CELERY_HOOK_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
}


def test_celery_worker_process_init_hook_calls_dispose_engine() -> None:
    """apps/workers celery_app.py wires worker_process_init -> dispose_engine()."""
    env = {**os.environ, **_CELERY_HOOK_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _CELERY_HOOK_WIRING_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (c) Pooler-safe config: prepare_threshold=None (FR-010)
# ---------------------------------------------------------------------------


def test_engine_build_path_disables_server_side_prepared_statements() -> None:
    """get_engine()'s connect_args disable psycopg's prepared-statement cache.

    Inspects the source of get_engine() rather than building a live engine,
    so no DB connection is required.
    """
    source = inspect.getsource(database.get_engine)
    assert '"prepare_threshold": None' in source or "'prepare_threshold': None" in source


def test_get_engine_passes_prepare_threshold_none_connect_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the actual connect_args create_engine() is called with, without connecting."""
    captured: dict[str, object] = {}

    def _fake_create_engine(url: str, **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(database, "_engine", None, raising=False)
    monkeypatch.setattr(database, "_sessionmaker", None, raising=False)
    monkeypatch.setattr(database, "create_engine", _fake_create_engine)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@pgbouncer:6432/db")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SCRAPYD_HTTP_URLS", "http://scrapers:6800")
    monkeypatch.setenv("SCRAPYD_BROWSER_URLS", "http://scrapers-browser:6800")
    monkeypatch.setenv("SCRAPYD_USERNAME", "scrapyd")
    monkeypatch.setenv("SCRAPYD_PASSWORD", "change-me")

    from app_shared.config import get_settings

    get_settings.cache_clear()
    try:
        database.get_engine()
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(database, "_engine", None, raising=False)
        monkeypatch.setattr(database, "_sessionmaker", None, raising=False)

    assert captured.get("connect_args") == {"prepare_threshold": None}
