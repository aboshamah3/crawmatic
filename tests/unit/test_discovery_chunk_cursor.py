"""`STRATEGY_DISCOVERY_SCAN` — the fleet-wide, chunked discovery re-drive
sweep and its durable cursor (EPA B4, F09, "resumable long maintenance").

Three layers, each tested at the layer where it actually lives:

1. **Pure pagination arithmetic** (`_plan_next_scan_chunk`) — no I/O,
   tested directly.
2. **Statement shape** (`_discovery_scan_query`,
   `_discovery_scan_cursor_upsert_stmt`) — rendered SQL asserted without
   executing it (the `tests/unit/test_rollup_watermark.py` precedent).
3. **Task behaviour** (`strategy_discovery_scan`) — every DB-touching
   collaborator monkeypatched on the module (the `tests/unit/
   test_breaker_evaluate_task.py` precedent), so the "chunked with a
   persisted cursor, re-enqueuing until complete" contract is asserted
   behaviourally: a full chunk advances the cursor and re-enqueues
   itself; a short/empty chunk resets the cursor and does NOT re-enqueue.

Subprocess-loaded because `apps/workers` ships its own top-level `app`
package and `celery_app.py` calls `get_settings()` at module scope (the
`test_celery_delivery_reliability.py` idiom).
"""

from __future__ import annotations

import os
import subprocess
import sys

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

_SETUP = """
import sys
sys.path.insert(0, "apps/workers")

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from app.workers import tasks_strategy
from app_shared.config import get_settings
from app_shared.task_names import STRATEGY_DISCOVERY_RUN, STRATEGY_DISCOVERY_SCAN
from app_shared.maintenance.scoping import maintenance_scope_of, MaintenanceScope


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _install(*, profile_rows, max_domains_per_run, cursor=None):
    '''Stub every DB-touching collaborator `strategy_discovery_scan` calls.
    `profile_rows` stands in for exactly what one chunk's query returned
    -- the test controls "the database" at that seam, the same
    `_fake_system_session` precedent `test_breaker_evaluate_task.py` uses.
    '''
    read_cursor = MagicMock(name="_read_discovery_scan_cursor", return_value=cursor)
    scan_refs = MagicMock(name="_scan_discovery_due_profile_refs", return_value=profile_rows)
    advance_cursor = MagicMock(name="_advance_discovery_scan_cursor")
    write_outbox = MagicMock(name="write_outbox_message")
    enqueue_fn = MagicMock(name="enqueue")
    settings = MagicMock(STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN=max_domains_per_run)

    @contextmanager
    def _fake_session_cm():
        yield MagicMock(name="session")

    @contextmanager
    def _fake_workspace_context(session, workspace_id):
        yield

    tasks_strategy.get_settings = lambda: settings
    tasks_strategy.get_system_session = _fake_session_cm
    tasks_strategy.get_session = _fake_session_cm
    tasks_strategy.workspace_context = _fake_workspace_context
    tasks_strategy._read_discovery_scan_cursor = read_cursor
    tasks_strategy._scan_discovery_due_profile_refs = scan_refs
    tasks_strategy._advance_discovery_scan_cursor = advance_cursor
    tasks_strategy.write_outbox_message = write_outbox
    tasks_strategy.enqueue = enqueue_fn
    return {
        "read_cursor": read_cursor,
        "scan_refs": scan_refs,
        "advance_cursor": advance_cursor,
        "write_outbox": write_outbox,
        "enqueue": enqueue_fn,
    }
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


# ---------------------------------------------------------------------------
# 1. Pure pagination arithmetic
# ---------------------------------------------------------------------------


def test_full_chunk_is_not_pass_complete_and_cursor_advances_to_last_id() -> None:
    _assert_ok(
        _run(
            """
ids = [uuid.uuid4() for _ in range(20)]
pass_complete, new_cursor = tasks_strategy._plan_next_scan_chunk(ids, chunk_size=20)
assert pass_complete is False
assert new_cursor == ids[-1]
print("OK")
"""
        )
    )


def test_short_chunk_is_pass_complete_and_cursor_resets_to_none() -> None:
    _assert_ok(
        _run(
            """
ids = [uuid.uuid4() for _ in range(5)]
pass_complete, new_cursor = tasks_strategy._plan_next_scan_chunk(ids, chunk_size=20)
assert pass_complete is True
assert new_cursor is None
print("OK")
"""
        )
    )


def test_empty_chunk_is_pass_complete_and_cursor_resets_to_none() -> None:
    _assert_ok(
        _run(
            """
pass_complete, new_cursor = tasks_strategy._plan_next_scan_chunk([], chunk_size=20)
assert pass_complete is True
assert new_cursor is None
print("OK")
"""
        )
    )


# ---------------------------------------------------------------------------
# 2. Statement shape (rendered SQL, no session at all)
# ---------------------------------------------------------------------------


def test_scan_query_filters_discovery_required_orders_by_id_and_limits() -> None:
    _assert_ok(
        _run(
            """
sql = _compiled(tasks_strategy._discovery_scan_query(cursor=None, limit=20))
assert "domain_strategy_profiles" in sql
assert "ORDER BY domain_strategy_profiles.id" in sql
assert "LIMIT" in sql
# No cursor supplied -- must not filter on id at all.
assert "domain_strategy_profiles.id >" not in sql
print("OK")
"""
        )
    )


def test_scan_query_with_a_cursor_only_returns_rows_strictly_after_it() -> None:
    _assert_ok(
        _run(
            """
cursor = uuid.uuid4()
stmt = tasks_strategy._discovery_scan_query(cursor=cursor, limit=20)
sql = _compiled(stmt)
assert "domain_strategy_profiles.id >" in sql
print("OK")
"""
        )
    )


def test_cursor_upsert_targets_the_global_key_and_increments_advance_count() -> None:
    _assert_ok(
        _run(
            """
from datetime import datetime, timezone
stmt = tasks_strategy._discovery_scan_cursor_upsert_stmt(
    cursor_profile_id=None, now=datetime(2026, 9, 7, tzinfo=timezone.utc)
)
sql = _compiled(stmt)
assert "strategy_discovery_state" in sql
assert "ON CONFLICT (key) DO UPDATE" in sql
assert "advance_count" in sql
print("OK")
"""
        )
    )


# ---------------------------------------------------------------------------
# 3. Task behaviour: chunked, cursor-driven, self-re-enqueuing
# ---------------------------------------------------------------------------


def test_full_chunk_enqueues_discovery_run_per_profile_and_reenqueues_the_scan() -> None:
    """The core "resumable long maintenance" contract: a full chunk means
    more profiles may remain, so the scan advances its cursor and
    re-enqueues ITSELF to keep going."""
    _assert_ok(
        _run(
            """
ids = [uuid.uuid4() for _ in range(3)]
ws = uuid.uuid4()
comp = uuid.uuid4()
rows = [(pid, ws, comp, "example.com", "example.com") for pid in ids]
mocks = _install(profile_rows=rows, max_domains_per_run=3)

tasks_strategy.strategy_discovery_scan()

assert mocks["write_outbox"].call_count == 3, mocks["write_outbox"].call_args_list
first_call_kwargs = mocks["write_outbox"].call_args_list[0].kwargs
assert first_call_kwargs["task_name"] == STRATEGY_DISCOVERY_RUN
assert first_call_kwargs["queue"] == "strategy_discovery"
assert first_call_kwargs["dedup_key"] == f"discovery:scan:{ids[0]}"
assert first_call_kwargs["kwargs"]["domain"] == "example.com"
assert first_call_kwargs["kwargs"]["triggered_by"] == "AUTO"

mocks["advance_cursor"].assert_called_once()
assert mocks["advance_cursor"].call_args.kwargs["cursor_profile_id"] == ids[-1]

mocks["enqueue"].assert_called_once_with(STRATEGY_DISCOVERY_SCAN, queue="maintenance")
print("OK")
"""
        )
    )


def test_short_chunk_resets_cursor_and_does_not_reenqueue() -> None:
    """Reaching the end of the table means the pass is done -- the
    cursor resets to the start (so a profile that flips back to
    DISCOVERY_REQUIRED behind an advanced cursor is caught on the NEXT
    pass), and there is nothing left to resume right now."""
    _assert_ok(
        _run(
            """
ids = [uuid.uuid4()]
ws = uuid.uuid4()
comp = uuid.uuid4()
rows = [(ids[0], ws, comp, "example.com", "example.com")]
mocks = _install(profile_rows=rows, max_domains_per_run=20)

tasks_strategy.strategy_discovery_scan()

assert mocks["write_outbox"].call_count == 1
mocks["advance_cursor"].assert_called_once()
assert mocks["advance_cursor"].call_args.kwargs["cursor_profile_id"] is None
mocks["enqueue"].assert_not_called()
print("OK")
"""
        )
    )


def test_empty_batch_writes_nothing_resets_cursor_and_does_not_reenqueue() -> None:
    _assert_ok(
        _run(
            """
mocks = _install(profile_rows=[], max_domains_per_run=20)

tasks_strategy.strategy_discovery_scan()

mocks["write_outbox"].assert_not_called()
assert mocks["advance_cursor"].call_args.kwargs["cursor_profile_id"] is None
mocks["enqueue"].assert_not_called()
print("OK")
"""
        )
    )


def test_scan_reads_the_persisted_cursor_and_passes_it_through() -> None:
    _assert_ok(
        _run(
            """
existing_cursor = uuid.uuid4()
mocks = _install(profile_rows=[], max_domains_per_run=20, cursor=existing_cursor)

tasks_strategy.strategy_discovery_scan()

mocks["scan_refs"].assert_called_once_with(cursor=existing_cursor, limit=20)
print("OK")
"""
        )
    )


def test_default_max_domains_per_run_is_20() -> None:
    _assert_ok(
        _run(
            """
assert get_settings().STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN == 20
print("OK")
"""
        )
    )


def test_task_is_registered_under_its_name_and_declares_a_fleet_scope() -> None:
    _assert_ok(
        _run(
            """
assert STRATEGY_DISCOVERY_SCAN in tasks_strategy.app.tasks
assert (
    maintenance_scope_of(tasks_strategy.app.tasks[STRATEGY_DISCOVERY_SCAN])
    is MaintenanceScope.FLEET
)
print("OK")
"""
        )
    )
