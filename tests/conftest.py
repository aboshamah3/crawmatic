"""Root test fixtures — the unit suite runs against a DELIBERATELY INVALID
environment (production-readiness audit §2, plan task 0.4).

Why
---

Before this file existed, `pytest tests/unit` inherited whatever was in the
developer's (or CI runner's) environment and `.env`: a `DATABASE_URL`
pointing at a real database, a live `REDIS_URL`, real Scrapyd nodes. Any
unit test that reached a dependency it was supposed to fake therefore
*passed* — quietly, by using the ambient stack — and the same test failed on
a machine without it. That makes the suite's green a statement about the
machine, not about the code.

The fix is to make the ambient environment unusable rather than absent:
every dependency URL is rewritten to a syntactically valid address on a port
nothing can be listening on (`127.0.0.1:1`, the reserved TCP port), so a
test that constructs a `Settings` or builds an engine still succeeds, while
a test that actually *connects* fails immediately and loudly with
`ConnectionRefusedError` instead of silently talking to something real.
"Invalid, not missing" matters: a missing required variable would fail at
`Settings()` construction, which hides the difference between "this test
needs config" and "this test needs a live server".

Integration tests are exempt
----------------------------

Tests marked `integration` need the real docker-compose stack, so the
ambient values are restored around each of them by a second, function-scoped
fixture (see `docs/ops/TESTING.md` for the two commands). The exemption is
PER TEST, not per session, deliberately: `tests/unit/` itself contains a
couple of `integration`-marked tests, so a session-wide stand-down would
silently disable the isolation for the ~4k unit tests running alongside them
whenever someone ran `pytest tests/unit` without `-m "not integration"`.

A test that genuinely needs a live dependency gets the `integration`
marker — the fixture is never weakened to accommodate it.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

#: Dependency endpoints rewritten for every non-integration session.
#:
#: Port 1 is reserved (`tcpmux`) and nothing binds it in practice, so a
#: connection attempt is refused immediately rather than hanging until a
#: timeout — a unit test that reaches out fails in milliseconds with a clear
#: `ConnectionRefusedError`.
INVALID_DEPENDENCY_ENVIRONMENT = {
    "DATABASE_URL": "postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "SCRAPYD_HTTP_URLS": "http://127.0.0.1:1",
    "SCRAPYD_BROWSER_URLS": "http://127.0.0.1:1",
}


def _clear_settings_cache() -> None:
    """Drop `app_shared.config.get_settings`'s `lru_cache`.

    `get_settings()` is memoised per process, so anything that read settings
    during collection (an import-time call in a module under test) would pin
    the pre-patch environment for the whole session. Cleared on the way in
    AND on the way out, so the session leaves no cached `Settings` built from
    the invalid values behind for whatever runs next in the same process.

    Guarded: `app_shared` not being importable is a real failure, but it is
    the *test's* failure to report, not this fixture's to raise during setup
    of every session.
    """
    try:
        from app_shared.config import get_settings
    except Exception:  # noqa: BLE001 - see docstring
        return
    get_settings.cache_clear()


#: Directories whose tests need the live docker-compose stack by convention.
#: Everything under them is marked `integration` at collection time (see
#: `pytest_collection_modifyitems`), because the repo has always separated
#: these suites by DIRECTORY while only a couple of files carry the marker
#: explicitly — and this file's exemption keys off the marker.
_INTEGRATION_DIRS = ("tests/integration",)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Make the directory convention explicit as the `integration` marker.

    `pyproject.toml` already describes the marker as "needs the
    docker-compose DB/pgbouncer stack; skipped by -m 'not integration'",
    which is exactly what every test under `tests/integration/` is — but only
    two of them say so with a decorator. Marking them here means:

    * `-m "not integration"` deselects them wherever they are collected from,
      matching the marker's documented meaning;
    * the environment isolation below (which is marker-driven, per plan task
      0.4) exempts them, so CI steps that run a file out of
      `tests/integration/` directly — `.github/workflows/ci.yml` runs several
      with no `-m` filter — keep the real stack they were pointed at.
    """
    for item in items:
        path = item.path.as_posix() if item.path is not None else ""
        if any(f"/{directory}/" in path for directory in _INTEGRATION_DIRS):
            item.add_marker("integration")


@pytest.fixture(scope="session", autouse=True)
def isolated_unit_environment() -> Iterator[dict[str, str | None]]:
    """Point every dependency URL at a dead port for the whole session.

    Session-scoped (the environment is process-wide state, and re-patching it
    per test would cost a `Settings` rebuild in ~4k tests) and autouse (a
    guarantee a test has to opt into is not a guarantee).

    `pytest.MonkeyPatch.context()` rather than the `monkeypatch` fixture:
    that fixture is function-scoped and cannot be requested from a
    session-scoped one. The context manager is the same machinery with the
    same restore-on-exit semantics.

    Yields the pre-patch values (``None`` where the variable was unset) so
    `_real_environment_for_integration_tests` can hand them back to the tests
    that are allowed to use them.
    """
    original = {name: os.environ.get(name) for name in INVALID_DEPENDENCY_ENVIRONMENT}

    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in INVALID_DEPENDENCY_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        _clear_settings_cache()
        try:
            yield original
        finally:
            _clear_settings_cache()


@pytest.fixture(autouse=True)
def _real_environment_for_integration_tests(
    request: pytest.FixtureRequest,
    isolated_unit_environment: dict[str, str | None],
) -> Iterator[None]:
    """Restore the ambient environment around an `integration`-marked test.

    Costs one marker lookup for every other test. The alternative — deciding
    once per session — would turn a single collected integration test into a
    session-wide loss of isolation, which is exactly the ambient-environment
    dependence this file exists to remove.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in isolated_unit_environment.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        _clear_settings_cache()
        try:
            yield
        finally:
            _clear_settings_cache()
