"""Import-boundary tests for the shared-library dependency direction (T043).

Enforces FR-003 / data-model.md "Entity: Shared Library Member":

* ``app_shared`` (and its submodules ``config``/``database``/``task_names``/
  ``ids``/``money``/``enums``/``models``/``models.base``/``models.rls``/
  ``models.identity``/``repository``/``security``, plus the SPEC-03
  ``security`` primitives as they land) MUST NOT pull in
  Scrapy/Twisted/Playwright — those belong only to the Scrapyd-side app
  members (``scrapers``, ``scrapers-browser``) and their shared
  ``scrape_core`` library. ``app_shared`` also MUST NOT pull in FastAPI
  (framework-agnostic; the FastAPI dependency + routers live only in
  ``apps/api``).
* ``app_shared`` MUST NOT depend on ``scrape_core`` — the dependency edge
  runs one way: ``scrape_core`` may import ``app_shared``, never the
  reverse.

Each check runs the import in a **fresh subprocess** (rather than just
inspecting ``sys.modules`` in-process) so that whatever the current test
process happens to have already imported (e.g. because the shared uv
workspace virtualenv also contains scrapy/twisted/playwright for the
scraper members) can never leak into the result and mask a real
boundary violation.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

FORBIDDEN_MODULES = ("scrapy", "twisted", "playwright", "fastapi")

# Import every app_shared submodule so a violation hiding in config.py,
# database.py, task_names.py, or the SPEC-02 primitives (ids/money/enums/
# models) is caught, not just __init__.py.
_APP_SHARED_IMPORT_CHECK = f"""
import sys

import app_shared
import app_shared.config
import app_shared.database
import app_shared.task_names
import app_shared.ids
import app_shared.money
import app_shared.enums
import app_shared.models
import app_shared.models.base
import app_shared.models.rls
import app_shared.models.identity
import app_shared.repository
import app_shared.security

forbidden = {FORBIDDEN_MODULES!r}
leaked = sorted(mod for mod in forbidden if mod in sys.modules)
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)

if "scrape_core" in sys.modules:
    print("LEAKED:scrape_core")
    sys.exit(1)

sys.exit(0)
"""

# Light check on the allowed direction: scrape_core MAY import app_shared
# without any conflict (no circular import, no boundary violation) even
# though the current skeleton's scrape_core/__init__.py doesn't need to
# import it yet (T016 — package marker only, no scraping logic).
_SCRAPE_CORE_IMPORT_CHECK = """
import sys

import scrape_core
import app_shared

if "scrape_core" not in sys.modules or "app_shared" not in sys.modules:
    print("MISSING")
    sys.exit(1)

sys.exit(0)
"""


def _run_in_subprocess(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_app_shared_does_not_import_scrapy_twisted_playwright() -> None:
    """Importing app_shared (and its submodules) never pulls in the scraping stack."""
    result = _run_in_subprocess(_APP_SHARED_IMPORT_CHECK)

    assert result.returncode == 0, (
        "app_shared import pulled in a forbidden module or scrape_core:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_app_shared_does_not_depend_on_scrape_core() -> None:
    """app_shared must never import scrape_core (no reverse dependency edge)."""
    # Runtime check: importing app_shared never pulls scrape_core into
    # sys.modules (covered by _APP_SHARED_IMPORT_CHECK above).
    result = _run_in_subprocess(_APP_SHARED_IMPORT_CHECK)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    # Static check: no source file under app_shared references
    # scrape_core at all, so the boundary can't be reintroduced later
    # via a lazy/deferred import that the runtime check wouldn't catch.
    import app_shared

    package_dir = pathlib.Path(app_shared.__file__).parent
    for source_file in package_dir.rglob("*.py"):
        contents = source_file.read_text(encoding="utf-8")
        assert "scrape_core" not in contents, (
            f"{source_file} references scrape_core — forbidden reverse edge"
        )


def test_scrape_core_may_import_app_shared() -> None:
    """The allowed direction: scrape_core is free to import app_shared."""
    result = _run_in_subprocess(_SCRAPE_CORE_IMPORT_CHECK)

    assert result.returncode == 0, (
        "scrape_core failed to import, or did not pull in app_shared:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
