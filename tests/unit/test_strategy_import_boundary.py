"""Import-boundary test for the SPEC-12 ``app_shared.strategy`` package (T042).

The strategy package is pure learning logic (SQLAlchemy / injected
``redis.Redis`` client / stdlib only). Like ``app_shared.access`` (SPEC-10)
and ``app_shared.alerts`` (SPEC-09), it MUST NOT pull in Scrapy/Twisted/
Playwright/FastAPI, nor import the application layers (``apps.*``/``app.*``)
— that would invert the dependency direction (Constitution I). The recorder's
raw per-attempt learning signal comes FROM the scrape-core pipeline; the
strategy package itself never imports ``scrape_core`` (the one-way edge also
asserted repo-wide in ``test_import_boundaries.py``).

Asserted by an AST scan of every module's own ``import``/``from ... import``
statements (not a ``sys.modules`` runtime check — ``app_shared.enums`` itself
legitimately imports sqlalchemy, which a transitive runtime check would
false-positive on; the SPEC-09/10 purity-check precedent).
"""

from __future__ import annotations

import ast
import pathlib

import app_shared.strategy

FORBIDDEN_ROOTS = frozenset(
    {"scrapy", "twisted", "playwright", "fastapi", "scrape_core", "apps", "app"}
)


def _strategy_package_dir() -> pathlib.Path:
    return pathlib.Path(app_shared.strategy.__file__).parent


def _imported_roots(source_path: pathlib.Path) -> set[str]:
    """Top-level package names this module imports (AST of its own statements)."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_strategy_package_has_no_forbidden_imports() -> None:
    package_dir = _strategy_package_dir()
    source_files = sorted(package_dir.rglob("*.py"))
    assert source_files, "expected app_shared.strategy to contain modules"

    for source_file in source_files:
        offending = _imported_roots(source_file) & FORBIDDEN_ROOTS
        assert not offending, (
            f"{source_file} imports forbidden root(s) {sorted(offending)} — "
            "app_shared.strategy must not depend on scrapy/twisted/playwright/"
            "fastapi/scrape_core/apps/app (Constitution I, FR-026 layering)"
        )


def test_strategy_package_reexports_public_surface() -> None:
    """The T042 public surface is importable straight from ``app_shared.strategy``."""
    for name in (
        "evaluate_promotion",
        "apply_promotion",
        "evaluate_rediscovery",
        "build_recent_signals",
        "apply_rediscovery",
        "resolve_strategy_start",
        "resolve_or_create_strategy_profile",
        "seed_from_discovery",
        "validate_sample_size",
        "record_attempt",
        "read_pending",
        "drain",
        "flush_profile",
        "resolve_profile",
        "stats_for_profile",
    ):
        assert hasattr(app_shared.strategy, name), f"missing re-export: {name}"
