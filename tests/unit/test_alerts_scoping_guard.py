"""Alerts CI scoping-guard unit tests (SPEC-09 T013, FR-005, SC-008).

Mirrors `tests/unit/test_observations_scoping_guard.py` (SPEC-07)
exactly, substituting `VariantPriceState`/`VariantAlertState`/
`PriceAlertEvent` for `PriceObservation`/`RequestAttempt`/
`MatchCurrentPrice`. Proves:

1. `scripts/check_workspace_scoping.py` exits **0** on the *real* repo
   tree now that the three SPEC-09 models are registered in
   `WORKSPACE_OWNED_MODELS` — landing them didn't introduce a real
   violation (no query call sites exist yet in this phase).
2. The guard's `select(...)`/`Session.get(...)` pattern-matching also
   catches an unscoped `select(VariantPriceState)` /
   `select(VariantAlertState)` / `select(PriceAlertEvent)` /
   `session.get(VariantPriceState, ...)` — planted in a throwaway
   fixture tree, per the same `scan(repo_root=...)` pattern as prior
   specs.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.check_workspace_scoping as guard  # noqa: E402

_ALERTS_VIOLATION_SOURCE = '''\
"""A fixture module planting unscoped alert model access
(a scoped_get/scoped_select omission)."""
from sqlalchemy import select

from app_shared.models.alerts import (
    PriceAlertEvent,
    VariantAlertState,
    VariantPriceState,
)


def unscoped_get_by_id(session, id_):
    return session.get(VariantPriceState, id_)


def unscoped_select_variant_price_states():
    return select(VariantPriceState)


def unscoped_select_variant_alert_states():
    return select(VariantAlertState)


def unscoped_select_price_alert_events():
    return select(PriceAlertEvent)
'''

_ALERTS_CLEAN_SOURCE = '''\
"""A fixture module using only sanctioned, scoped alert access
patterns."""
from sqlalchemy import select

from app_shared.models.alerts import PriceAlertEvent, VariantPriceState
from app_shared.repository import scoped_select


def scoped_by_helper(workspace_id):
    return scoped_select(VariantPriceState, workspace_id)


def scoped_by_explicit_where(workspace_id):
    return select(PriceAlertEvent).where(PriceAlertEvent.workspace_id == workspace_id)
'''


def _write_fixture(tmp_path: Path, source: str, filename: str = "sample_module.py") -> Path:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = apps_dir / filename
    fixture_path.write_text(source, encoding="utf-8")
    return fixture_path


# --- real repo tree: exits 0 ------------------------------------------------


def test_guard_exits_0_on_the_real_repo_tree() -> None:
    """The guard's real `apps/`+`libs/` scan (this repo) is clean — no
    unscoped alerts query exists yet (this phase adds no call sites)."""
    assert guard.main([]) == 0


def test_guard_scan_finds_no_violations_on_the_real_repo_tree() -> None:
    violations = guard.scan()
    assert violations == [], "\n".join(str(v) for v in violations)


def test_alerts_models_are_registered_as_workspace_owned() -> None:
    from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    for model in (VariantPriceState, VariantAlertState, PriceAlertEvent):
        assert model in WORKSPACE_OWNED_MODELS


# --- planted unscoped select(...) / session.get(...) ------------------------


def test_guard_flags_planted_unscoped_select_and_session_get(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path, _ALERTS_VIOLATION_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert len(violations) == 4
    reasons = "\n".join(v.reason for v in violations)
    assert "VariantPriceState" in reasons
    assert "get(VariantPriceState" in reasons or "get(VariantPriceState, ...)" in reasons
    assert "select(VariantPriceState)" in reasons
    assert "select(VariantAlertState)" in reasons
    assert "select(PriceAlertEvent)" in reasons


def test_guard_passes_clean_scoped_alerts_snippets(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _ALERTS_CLEAN_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []


def test_guard_main_exits_non_zero_on_planted_alerts_violation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation_root = tmp_path / "violation"
    _write_fixture(violation_root, _ALERTS_VIOLATION_SOURCE)

    original_scan = guard.scan
    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=violation_root))

    assert guard.main([]) != 0
    out = capsys.readouterr().out
    assert "select(VariantPriceState)" in out
