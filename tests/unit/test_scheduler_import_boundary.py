"""Import-boundary test for the SPEC-13 scheduler feature (T027, FR-019,
quickstart.md Scenario 8).

FR-019: "The scheduler service, running from the existing scheduler app,
MUST NOT introduce Scrapy/Twisted/Playwright dependencies into the shared
library or its own image (scraping-free scheduling path)."

Covers three surfaces, each already carrying its own "scraping-free"
docstring promise that this test enforces:

* ``app_shared.scheduling`` (``cadence.py``) -- pure cadence math (stdlib +
  ``croniter``), no FastAPI/Scrapy/Twisted/Playwright/SQLAlchemy-session
  machinery. AST-checked, like the SPEC-09 ``alerts`` / SPEC-10 ``access`` /
  SPEC-12 ``strategy`` purity checks -- a transitive runtime check would
  false-positive because sibling ``app_shared`` modules legitimately import
  sqlalchemy.
* ``app_shared.jobs.scopes`` -- pure query logic over
  ``CompetitorProductMatch`` (sqlalchemy is expected here, it's a query
  helper module), but never scrapy/twisted/playwright/fastapi. AST-checked.
* ``apps/scheduler`` (``app.scheduler.refresh`` + ``app.scheduler.scheduler_app``)
  -- the scheduler process's own import closure never pulls scrapy/twisted/
  playwright into ``sys.modules``. Runtime-checked in a **fresh subprocess**
  with ``sys.path.insert(0, "apps/scheduler")`` ahead of the import --
  mirrors the ``test_refresh_pass_isolation.py`` / ``test_jobs_dispatch_task.py``
  idiom: ``apps/api``, ``apps/scheduler``, and ``apps/workers`` each ship
  their own top-level ``app`` package, so a plain ``import app.scheduler.refresh``
  in the shared test process resolves ambiguously to whichever ``app``
  package another test module already imported.

Also re-runs the existing repo-wide ``tests/unit/test_import_boundaries.py``
suite as a subprocess to confirm ``croniter`` (T001, added to
``libs/shared/pyproject.toml`` for this spec) introduced no new forbidden
import into ``app_shared`` -- belt-and-suspenders on top of CI already
running that file directly.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

FORBIDDEN_ROOTS = frozenset({"scrapy", "twisted", "playwright", "fastapi"})


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _imported_roots(source_path: pathlib.Path) -> set[str]:
    """AST-parse ``source_path`` and return the set of top-level root
    module names it imports (``import a.b.c`` / ``from a.b import c`` both
    contribute root ``a``). Static, not a runtime ``sys.modules`` check --
    see the module docstring for why that distinction matters here (mirrors
    ``test_import_boundaries.py``'s ``_imported_root_modules`` /
    ``test_strategy_import_boundary.py``'s ``_imported_roots``).
    """
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


def test_scheduling_package_has_no_forbidden_imports() -> None:
    """``app_shared.scheduling`` (cadence math) is scraping-free (FR-019)."""
    import app_shared.scheduling

    package_dir = pathlib.Path(app_shared.scheduling.__file__).parent
    source_files = sorted(package_dir.rglob("*.py"))
    assert source_files, "expected app_shared.scheduling to contain modules"

    for source_file in source_files:
        offending = _imported_roots(source_file) & FORBIDDEN_ROOTS
        assert not offending, (
            f"{source_file} imports forbidden root(s) {sorted(offending)} -- "
            "app_shared.scheduling must not depend on scrapy/twisted/"
            "playwright/fastapi (FR-019)"
        )


def test_jobs_scopes_module_has_no_forbidden_imports() -> None:
    """``app_shared.jobs.scopes`` (scope -> active-match resolution) is
    scraping-free (FR-019). sqlalchemy is expected (it's a query-helper
    module); only the scraping stack + fastapi are forbidden."""
    import app_shared.jobs.scopes

    source_file = pathlib.Path(app_shared.jobs.scopes.__file__)
    offending = _imported_roots(source_file) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{source_file} imports forbidden root(s) {sorted(offending)} -- "
        "app_shared.jobs.scopes must not depend on scrapy/twisted/"
        "playwright/fastapi (FR-019)"
    )


def test_refresh_rules_model_has_no_forbidden_imports() -> None:
    """``app_shared.models.refresh_rules`` (the ORM model, T006) is free to
    import sqlalchemy (it's an ORM module) but must never import scrapy/
    twisted/playwright/fastapi."""
    import app_shared.models.refresh_rules

    source_file = pathlib.Path(app_shared.models.refresh_rules.__file__)
    offending = _imported_roots(source_file) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{source_file} imports forbidden root(s) {sorted(offending)} -- "
        "app_shared.models.refresh_rules must not depend on scrapy/twisted/"
        "playwright/fastapi (FR-019)"
    )


_SCHEDULER_APP_IMPORT_CHECK = """
import sys
sys.path.insert(0, "apps/scheduler")

import app.scheduler.refresh
import app.scheduler.scheduler_app

forbidden = {"scrapy", "twisted", "playwright", "fastapi"}
leaked = sorted(mod for mod in forbidden if mod in sys.modules)
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)

sys.exit(0)
"""


def _run_in_subprocess(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_repo_root()),
    )


def test_scheduler_app_does_not_import_scrapy_twisted_playwright() -> None:
    """Importing ``apps/scheduler``'s ``app.scheduler.refresh`` +
    ``app.scheduler.scheduler_app`` never pulls the scraping stack (or
    fastapi) into ``sys.modules`` (FR-019). Run in a fresh subprocess with
    ``apps/scheduler`` prepended to ``sys.path`` so the ambiguous top-level
    ``app`` package resolves to the scheduler's own, not another app's."""
    result = _run_in_subprocess(_SCHEDULER_APP_IMPORT_CHECK)
    assert result.returncode == 0, (
        "apps/scheduler pulled in a forbidden module:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_scheduler_refresh_source_has_no_forbidden_imports() -> None:
    """Static AST check on ``apps/scheduler``'s own source, so the boundary
    can't be reintroduced later via a lazy/deferred import the runtime
    subprocess check wouldn't catch."""
    repo_root = _repo_root()
    for relative in (
        "apps/scheduler/app/scheduler/refresh.py",
        "apps/scheduler/app/scheduler/scheduler_app.py",
    ):
        source_file = repo_root / relative
        offending = _imported_roots(source_file) & FORBIDDEN_ROOTS
        assert not offending, (
            f"{source_file} imports forbidden root(s) {sorted(offending)} -- "
            "apps/scheduler must stay scraping-free (FR-019)"
        )


def test_existing_import_boundaries_suite_still_passes() -> None:
    """Confirm ``croniter`` (T001) introduced no new forbidden import into
    ``app_shared`` -- re-run the repo-wide import-boundary suite as a
    subprocess (belt-and-suspenders on top of CI running it directly)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_import_boundaries.py", "-q"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_repo_root()),
    )
    assert result.returncode == 0, (
        "tests/unit/test_import_boundaries.py failed after croniter was added:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
