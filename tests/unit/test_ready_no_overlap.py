"""EPA B8 (F16): `/ready` probes cannot pile up under concurrent load.

`apps.api.app.routers.ready` runs every probe against ONE module-level
`ThreadPoolExecutor(max_workers=2)` and guards a full probe "generation"
with a non-blocking lock: a request that finds a generation already in
flight returns the LAST completed generation's result with
`"stale": true` instead of queuing its own probes behind it. This proves
three things a concurrent orchestrator hammering `/ready` depends on:

1. No matter how many `/ready` calls arrive while one dependency is
   slow, at most `max_workers=2` probe threads ever exist.
2. All but the one call that actually ran the generation get the cached
   result back marked `stale: true`.
3. The database probe and the migration probe never share a session
   object — each opens its own via `get_session()` (distinct ids).
"""

from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Response

from app.routers import ready


class _FakeSession:
    """Same query-dispatch shape as `test_ready_endpoint.py`'s fake, minus
    any blocking behaviour — used for the migration probe and for the
    seeding call."""

    def __init__(self, *, head: str = "test_head_0001") -> None:
        self._head = head

    def execute(self, statement: object = None, *_a: object, **_k: object) -> object:
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _AlembicResult(self._head)
        return None


class _AlembicResult:
    def __init__(self, head: str) -> None:
        self._head = head

    def first(self) -> tuple[str]:
        return (self._head,)


class _FakeRedis:
    def ping(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _pin_code_migration_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from app_shared import heartbeat as heartbeat_mod
    from app_shared import release as release_mod

    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "test_head_0001")
    monkeypatch.delenv(heartbeat_mod.REQUIRED_SERVICES_ENV, raising=False)
    release_mod.reset_release_identity_cache()


@pytest.fixture(autouse=True)
def _reset_generation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a clean cache/lock — this module state is otherwise
    process-wide and would leak between tests in the same pytest run."""
    monkeypatch.setattr(ready, "_last_response", None)
    monkeypatch.setattr(ready, "_last_status_code", 503)
    # A fresh, never-contended lock per test, so a previous test's
    # generation can never still be "held" here.
    monkeypatch.setattr(ready, "_generation_lock", threading.Lock())


def _call_ready() -> tuple[object, int]:
    """Invoke the `/ready` route function directly (not over HTTP) — this
    is the same code path FastAPI calls per request, and driving it
    in-thread avoids any TestClient-level threading assumptions the
    concurrency assertions below don't need."""
    response = Response()
    body = ready.ready(response)
    return body, response.status_code


def test_no_overlap_bounds_threads_and_marks_pileup_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed the cache with one clean, fast generation so the 19 losers below
    # have a real prior result to fall back on.
    monkeypatch.setattr(ready, "get_session", lambda: contextlib.nullcontext(_FakeSession()))
    monkeypatch.setattr(ready, "get_redis_client", lambda: _FakeRedis())
    seeded_body, seeded_status = _call_ready()
    assert seeded_status == 200
    assert seeded_body.stale is False

    # Now make the database probe block until released, and count how many
    # times it (and the migration probe) actually ran, plus every session
    # object handed out — the migration probe must get a DIFFERENT one.
    block_started = threading.Event()
    release_block = threading.Event()
    call_count = {"n": 0}
    call_lock = threading.Lock()
    session_ids: list[int] = []

    def _blocking_get_session() -> contextlib.AbstractContextManager:
        with call_lock:
            call_count["n"] += 1
            first_call = call_count["n"] == 1
        session = _FakeSession()
        session_ids.append(id(session))
        if first_call:
            block_started.set()
            release_block.wait(timeout=5)
        return contextlib.nullcontext(session)

    monkeypatch.setattr(ready, "get_session", _blocking_get_session)

    results: list[tuple[object, int]] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        result = _call_ready()
        with results_lock:
            results.append(result)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_worker) for _ in range(20)]
        # Give the winner time to acquire the generation lock and start
        # blocking inside the database probe.
        assert block_started.wait(timeout=5), "no request ever started a generation"

        # Give every one of the other 19 threads (already started, or still
        # spinning up) time to attempt the lock and lose, before the
        # winner's generation is allowed to complete — otherwise a slow-to
        # -start thread could race in as a SECOND generation after the
        # first one finishes, rather than observing the pileup.
        time.sleep(0.5)

        # Invariant 1: at most 2 probe worker threads exist for this whole
        # router, no matter how many `/ready` calls are in flight.
        assert len(ready._PROBE_EXECUTOR._threads) <= 2

        release_block.set()
        for future in futures:
            future.result(timeout=5)

    assert len(results) == 20
    stale_count = sum(1 for body, _ in results if body.stale is True)
    fresh_count = len(results) - stale_count

    # Invariant 2: exactly one request actually ran the generation; the
    # other 19 got the cached result marked stale.
    assert stale_count == 19
    assert fresh_count == 1

    # Invariant 3: the database probe's session and the migration probe's
    # session are never the same object.
    assert len(session_ids) >= 2
    assert len(set(session_ids)) == len(session_ids), "a session object was reused across probes"
