"""Catalog CI scoping-guard unit tests (SPEC-04 T029, FR-002/SC-006).

Two things this test proves that `tests/unit/test_workspace_scoping_guard.py`
(SPEC-03) does not:

1. `scripts/check_workspace_scoping.py` exits **0** on the *real* repo
   tree now that the four catalog models
   (`Product`/`ProductVariant`/`ProductGroup`/`ProductGroupItem`) are
   registered in `WORKSPACE_OWNED_MODELS` and every catalog router uses
   `scoped_select`/`scoped_get` — i.e. landing SPEC-04's routers didn't
   introduce a real violation.
2. The guard's `select(...)`/`Session.get(...)` pattern-matching also
   catches an unscoped `select(Product)` specifically (not just the
   SPEC-03 `User`/`ApiKey` models it was originally proven against) —
   planted in a throwaway fixture tree, per the same `scan(repo_root=...)`
   pattern as the SPEC-03 test.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.check_workspace_scoping as guard  # noqa: E402

_PRODUCT_VIOLATION_SOURCE = '''\
"""A fixture module planting an unscoped select(Product) and Session.get(Product, ...)."""
from sqlalchemy import select

from app_shared.models.catalog import Product


def unscoped_get_by_id(session, id_):
    return session.get(Product, id_)


def unscoped_select_products():
    return select(Product)
'''

_PRODUCT_CLEAN_SOURCE = '''\
"""A fixture module using only sanctioned, scoped catalog access patterns."""
from sqlalchemy import select

from app_shared.models.catalog import Product
from app_shared.repository import scoped_select


def scoped_by_helper(workspace_id):
    return scoped_select(Product, workspace_id)


def scoped_by_explicit_where(workspace_id):
    return select(Product).where(Product.workspace_id == workspace_id)
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
    catalog query goes through `scoped_select`/`scoped_get`."""
    assert guard.main([]) == 0


def test_guard_scan_finds_no_violations_on_the_real_repo_tree() -> None:
    violations = guard.scan()
    assert violations == [], "\n".join(str(v) for v in violations)


def test_four_catalog_models_are_registered_as_workspace_owned() -> None:
    from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    for model in (Product, ProductVariant, ProductGroup, ProductGroupItem):
        assert model in WORKSPACE_OWNED_MODELS


# --- planted unscoped select(Product) is flagged ----------------------------


def test_guard_flags_planted_unscoped_select_product_and_session_get(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path, _PRODUCT_VIOLATION_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert len(violations) == 2
    reasons = "\n".join(v.reason for v in violations)
    assert "Product" in reasons
    assert "get(Product" in reasons or "get(Product, ...)" in reasons
    assert "select(Product)" in reasons


def test_guard_passes_clean_scoped_product_snippets(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _PRODUCT_CLEAN_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []


def test_guard_main_exits_non_zero_on_planted_product_violation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation_root = tmp_path / "violation"
    _write_fixture(violation_root, _PRODUCT_VIOLATION_SOURCE)

    original_scan = guard.scan
    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=violation_root))

    assert guard.main([]) != 0
    out = capsys.readouterr().out
    assert "select(Product)" in out
