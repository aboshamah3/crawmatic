"""``scrape_core.targets.load_targets`` F-2 belt-and-braces: duplicate
spider runs must fetch nothing already terminal (2026-08-22 incident --
a duplicate ``load_targets`` call re-fetched work `dispatch` had already
finished, producing +7,190 duplicate price observations and paid proxy
traffic).

When ``scrape_job_id`` is given, any ``match_id`` whose
``scrape_job_targets`` row for that job is already COMPLETED/FAILED/
SKIPPED is dropped *before* any resolution/fetch work -- one cheap
scoped ``IN`` query, independent of whatever caused dispatch to
misfire.

Exercised entirely against a fake session (the same
``_FakeSession``/``_FakeWorkspaceTxn`` pattern
``tests/unit/test_persistence_batching.py`` established for
``scrape_core`` DB-touching functions) -- no real DB. The fake
recognizes which query is the terminal-status probe by compiling each
statement (``tests/unit/test_retention_eligibility.py``'s
``literal_binds`` idiom) and inspecting it for the
``scrape_job_targets`` table name; every other query returns no rows,
so ``load_targets`` takes its normal "no matches found" early return
right after the filter runs -- exactly what this test needs to observe
(which match_ids survived the filter), without faking the rest of the
bounded-load pipeline.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from scrape_core import targets as targets_mod

_LOGGER_NAME = "scrape_core.targets"


class _FakeSettings:
    """Stand-in for ``app_shared.config.Settings`` -- just the one knob
    ``load_targets`` reads before entering ``workspace_txn``
    (``STRATEGY_PROFILE_SCOPE``). ``load_targets`` does a fresh, local
    ``from app_shared.config import get_settings`` on every call, so
    patching the real ``app_shared.config.get_settings`` name (rather
    than anything on ``targets_mod``) is what that local import actually
    resolves to (``test_observability_logs.py``'s ``_patch_get_settings``
    established this exact idiom for this module) -- this suite never
    constructs a real ``Settings()``, which would require every
    ``DATABASE_URL``/``REDIS_URL``/... env var to be set."""

    STRATEGY_PROFILE_SCOPE = "domain"


def _compiled(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _json_events(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != logger_name:
            continue
        try:
            payload = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and "event" in payload:
            events.append(payload)
    return events


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Records every executed statement; returns ``terminal_match_ids``
    for the ``scrape_job_targets`` probe and no rows for anything else
    (no real DB, mirrors ``test_persistence_batching.py``'s fake)."""

    def __init__(self, terminal_match_ids: list[uuid.UUID]) -> None:
        self.executed: list[Any] = []
        self._terminal_match_ids = terminal_match_ids

    def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        if "scrape_job_targets" in _compiled(stmt):
            return _FakeResult(self._terminal_match_ids)
        return _FakeResult([])

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.workspace_id: Any = None

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        self.workspace_id = workspace_id
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def test_load_targets_skips_terminal_targets_for_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """m1 COMPLETED, m2 FAILED, m3 PENDING for job J -- only m3's
    match_id should reach the next (matches) query, and the skip event
    should record ``skipped=2``."""
    caplog.set_level(logging.INFO)
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    m1, m2, m3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    session = _FakeSession(terminal_match_ids=[m1, m2])
    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    result = targets_mod.load_targets(workspace_id, [m1, m2, m3], scrape_job_id=job_id)

    assert result.targets == []
    # Two statements: the terminal-status probe, then the (narrowed)
    # matches query -- load_targets takes its "no matches found" early
    # return right after, since the fake returns no rows for it.
    assert len(session.executed) == 2
    matches_query_sql = _compiled(session.executed[1])
    assert str(m3) in matches_query_sql
    assert str(m1) not in matches_query_sql
    assert str(m2) not in matches_query_sql

    events = _json_events(caplog, _LOGGER_NAME)
    skip_events = [e for e in events if e["event"] == "dispatch.duplicate_terminal_skipped"]
    assert len(skip_events) == 1
    assert skip_events[0]["skipped"] == 2
    assert skip_events[0]["workspace_id"] == str(workspace_id)
    assert skip_events[0]["scrape_job_id"] == str(job_id)


def test_load_targets_returns_empty_when_all_targets_terminal_for_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every match_id already terminal for the job -- load_targets must
    return empty WITHOUT ever issuing the matches query (no
    resolution/fetch work of any kind for a fully-duplicate run)."""
    caplog.set_level(logging.INFO)
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    m1, m2 = uuid.uuid4(), uuid.uuid4()

    session = _FakeSession(terminal_match_ids=[m1, m2])
    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    result = targets_mod.load_targets(workspace_id, [m1, m2], scrape_job_id=job_id)

    assert result.targets == []
    # Only the terminal-status probe ran -- the early return fired
    # before any further query was issued.
    assert len(session.executed) == 1

    events = _json_events(caplog, _LOGGER_NAME)
    skip_events = [e for e in events if e["event"] == "dispatch.duplicate_terminal_skipped"]
    assert len(skip_events) == 1
    assert skip_events[0]["skipped"] == 2


def test_load_targets_without_scrape_job_id_does_not_probe_terminal_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Backward compatibility: existing callers that never pass
    ``scrape_job_id`` (default ``None``) get no terminal-status
    filtering at all -- the first (and only, given the fake) query is
    still the matches query, not a ``scrape_job_targets`` probe."""
    caplog.set_level(logging.INFO)
    workspace_id = uuid.uuid4()
    m1 = uuid.uuid4()

    session = _FakeSession(terminal_match_ids=[])
    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    result = targets_mod.load_targets(workspace_id, [m1])

    assert result.targets == []
    assert len(session.executed) == 1
    assert "scrape_job_targets" not in _compiled(session.executed[0])

    events = _json_events(caplog, _LOGGER_NAME)
    assert not [e for e in events if e["event"] == "dispatch.duplicate_terminal_skipped"]


# --- both spiders must actually PASS `scrape_job_id` -------------------------
#
# The filter above is dead weight in any spider that calls `load_targets`
# without the kwarg (it defaults to `None` -> no probe). The browser
# spider shipped without it (F-8, 2026-08-22 review) on the *expensive*
# path: browser scrapes carry proxy + headless cost, so a duplicate run
# there is worth several HTTP ones. An AST check, not a substring match,
# so a `scrape_job_id` mentioned anywhere else in the file cannot satisfy
# it.

_SPIDER_SOURCES = (
    "apps/scrapers/price_monitor/spiders/generic_price_spider.py",
    "apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py",
)


@pytest.mark.parametrize("relative_path", _SPIDER_SOURCES)
def test_spider_passes_scrape_job_id_to_load_targets(relative_path: str) -> None:
    import ast
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    tree = ast.parse((repo_root / relative_path).read_text(encoding="utf-8"))

    load_target_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            isinstance(arg, ast.Name) and arg.id == "load_targets"
            for arg in node.args
        )
    ]
    assert load_target_calls, f"no `load_targets` dispatch found in {relative_path}"

    for call in load_target_calls:
        assert "scrape_job_id" in {kw.arg for kw in call.keywords}, (
            f"{relative_path}:{call.lineno} calls load_targets without "
            "scrape_job_id -- the duplicate-run filter is a no-op there"
        )
