"""Competitors/matches CI scoping-guard unit tests (SPEC-05 US4 T027, FR-001/FR-002/SC-008).

Mirrors `tests/unit/test_catalog_scoping_guard.py` (SPEC-04) exactly,
substituting `Competitor`/`CompetitorProductMatch` for
`Product`/`ProductGroup`/etc. Two things this test proves that
`tests/unit/test_workspace_scoping_guard.py` (SPEC-03) does not:

1. `scripts/check_workspace_scoping.py` exits **0** on the *real* repo
   tree now that `Competitor`/`CompetitorProductMatch` are registered in
   `WORKSPACE_OWNED_MODELS` and every competitors/matches router uses
   `scoped_select`/`scoped_get` — i.e. landing SPEC-05's routers didn't
   introduce a real violation.
2. The guard's `select(...)`/`Session.get(...)` pattern-matching also
   catches an unscoped `select(Competitor)` / `select(CompetitorProductMatch)`
   / `session.get(Competitor, ...)` — an app-layer `scoped_get`/`scoped_select`
   omission — planted in a throwaway fixture tree, per the same
   `scan(repo_root=...)` pattern as the SPEC-03/04 tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.check_workspace_scoping as guard  # noqa: E402

_COMPETITOR_VIOLATION_SOURCE = '''\
"""A fixture module planting unscoped competitor/match model access
(a scoped_get/scoped_select omission)."""
from sqlalchemy import select

from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch


def unscoped_get_by_id(session, id_):
    return session.get(Competitor, id_)


def unscoped_select_competitors():
    return select(Competitor)


def unscoped_select_matches():
    return select(CompetitorProductMatch)
'''

_COMPETITOR_CLEAN_SOURCE = '''\
"""A fixture module using only sanctioned, scoped competitor/match access
patterns."""
from sqlalchemy import select

from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.repository import scoped_select


def scoped_by_helper(workspace_id):
    return scoped_select(Competitor, workspace_id)


def scoped_by_explicit_where(workspace_id):
    return select(CompetitorProductMatch).where(
        CompetitorProductMatch.workspace_id == workspace_id
    )
'''


def _write_fixture(tmp_path: Path, source: str, filename: str = "sample_module.py") -> Path:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = apps_dir / filename
    fixture_path.write_text(source, encoding="utf-8")
    return fixture_path


# --- real repo tree: exits 0 ------------------------------------------------


def test_guard_exits_0_on_the_real_repo_tree() -> None:
    """The guard's real `apps/`+`libs/` scan (this repo) is clean — every
    competitors/matches query goes through `scoped_select`/`scoped_get`."""
    assert guard.main([]) == 0


def test_guard_scan_finds_no_violations_on_the_real_repo_tree() -> None:
    violations = guard.scan()
    assert violations == [], "\n".join(str(v) for v in violations)


def test_competitor_and_match_models_are_registered_as_workspace_owned() -> None:
    from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    for model in (Competitor, CompetitorProductMatch):
        assert model in WORKSPACE_OWNED_MODELS


# --- planted unscoped select(Competitor)/select(CompetitorProductMatch) -----


def test_guard_flags_planted_unscoped_select_and_session_get(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path, _COMPETITOR_VIOLATION_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert len(violations) == 3
    reasons = "\n".join(v.reason for v in violations)
    assert "Competitor" in reasons
    assert "get(Competitor" in reasons or "get(Competitor, ...)" in reasons
    assert "select(Competitor)" in reasons
    assert "select(CompetitorProductMatch)" in reasons


def test_guard_passes_clean_scoped_competitor_match_snippets(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _COMPETITOR_CLEAN_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []


def test_guard_main_exits_non_zero_on_planted_competitor_match_violation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation_root = tmp_path / "violation"
    _write_fixture(violation_root, _COMPETITOR_VIOLATION_SOURCE)

    original_scan = guard.scan
    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=violation_root))

    assert guard.main([]) != 0
    out = capsys.readouterr().out
    assert "select(Competitor)" in out
    assert "select(CompetitorProductMatch)" in out
