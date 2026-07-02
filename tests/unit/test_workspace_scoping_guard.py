"""CI scoping-guard unit tests (SPEC-03 T042, FR-020/SC-006).

Exercises `scripts/check_workspace_scoping.py`'s `scan()` directly
against temporary fixture trees (never the real repo — that executable
validation is T044) so this is pure, fast, and DB/Redis-independent.

`scan(repo_root=...)` accepts an explicit root, so the guard's real
`apps/`+`libs/` scan of THIS repo is left untouched; each test builds
its own throwaway `<tmp>/apps/...py` fixture. Fixture files are
deliberately named/placed OUTSIDE any `tests/` directory and without a
`test_` prefix, since the guard treats test files as allowlisted
(exempt) — a planted violation living in what the guard would consider
a test file would never be flagged, defeating the test.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.check_workspace_scoping as guard  # noqa: E402


_VIOLATION_SOURCE = '''\
"""A fixture module planting two unscoped workspace-owned model accesses."""
from sqlalchemy import select

from app_shared.models.identity import ApiKey, User


def unscoped_get_by_id(session, id_):
    return session.get(User, id_)


def unscoped_select_api_keys():
    return select(ApiKey)
'''

_CLEAN_SOURCE = '''\
"""A fixture module using only sanctioned, scoped access patterns."""
from sqlalchemy import select

from app_shared.models.identity import ApiKey, User
from app_shared.repository import scoped_select


def scoped_by_helper(workspace_id):
    return scoped_select(User, workspace_id)


def scoped_by_explicit_where(workspace_id):
    return select(ApiKey).where(ApiKey.workspace_id == workspace_id)
'''


def _write_fixture(tmp_path: Path, source: str, filename: str = "sample_module.py") -> Path:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = apps_dir / filename
    fixture_path.write_text(source, encoding="utf-8")
    return fixture_path


def test_guard_flags_unscoped_session_get_and_unscoped_select(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _VIOLATION_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert len(violations) == 2
    reasons = "\n".join(v.reason for v in violations)
    assert "session.get(User" in reasons or "Session.get(User" in reasons
    assert "select(ApiKey)" in reasons


def test_guard_passes_clean_scoped_snippets(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _CLEAN_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []


def test_guard_main_exits_non_zero_on_violation_and_zero_on_clean(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation_root = tmp_path / "violation"
    clean_root = tmp_path / "clean"
    _write_fixture(violation_root, _VIOLATION_SOURCE)
    _write_fixture(clean_root, _CLEAN_SOURCE)

    original_scan = guard.scan

    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=violation_root))
    assert guard.main([]) != 0

    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=clean_root))
    assert guard.main([]) == 0
