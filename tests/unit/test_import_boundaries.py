"""Import-boundary tests for the shared-library dependency direction (T043,
T045, T050, SPEC-08 T049).

Enforces FR-003 / data-model.md "Entity: Shared Library Member":

* ``app_shared`` (and its submodules ``config``/``database``/``task_names``/
  ``ids``/``money``/``enums``/``models``/``models.base``/``models.rls``/
  ``models.identity``/``models.catalog``/``models.competitors_matches``/
  ``models.scrape_profiles``/``models.observations``/``models.jobs``/
  ``pagination``/``catalog``/``repository``/``security``/``url_safety``/
  ``url_pattern``/``matches``/``matches.upsert``/``profiles``/
  ``profiles.validation``/``profiles.confidence``/``profiles.resolution``/
  ``profiles.repository``/``profiles.upsert``/``scrapyd``/
  ``scrapyd.client``/``messaging``/``jobs``/``jobs.batching``/
  ``jobs.nodes``/``jobs.lifecycle``/``jobs.targets``/``jobs.service``,
  plus the SPEC-03 ``security`` primitives, the SPEC-04 catalog core, the
  SPEC-05 competitors/matches core, the SPEC-06 scrape-profiles core, the
  SPEC-07 observations models + Scrapyd dispatch client, and the SPEC-08
  jobs/orchestration core + messaging seam as they land) MUST NOT pull in
  Scrapy/Twisted/Playwright — those belong only to the Scrapyd-side app
  members (``scrapers``, ``scrapers-browser``) and their shared
  ``scrape_core`` library. ``app_shared`` also MUST NOT pull in FastAPI
  (framework-agnostic; the FastAPI dependency + routers live only in
  ``apps/api``). ``app_shared.messaging`` is the one seam allowed to pull
  in ``celery`` (the ban is scrapy/twisted/playwright/fastapi only).
* ``app_shared`` MUST NOT depend on ``scrape_core`` — the dependency edge
  runs one way: ``scrape_core`` may import ``app_shared`` (and, unlike
  ``app_shared``, scrape_core MAY import Scrapy/Twisted — that's the
  scraping runtime it wraps), never the reverse.
* ``apps/api/app/routers/jobs.py`` (SPEC-08) imports
  ``app_shared.jobs.service``/``app_shared.messaging`` to create jobs and
  enqueue dispatch work, but MUST NOT import ``apps.workers`` — the API
  never pulls in the worker's (and its future scrapy-adjacent) import
  closure (Constitution I).

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
import app_shared.models.catalog
import app_shared.models.competitors_matches
import app_shared.models.scrape_profiles
import app_shared.models.observations
import app_shared.pagination
import app_shared.catalog
import app_shared.repository
import app_shared.redis_client
import app_shared.security
import app_shared.security.passwords
import app_shared.security.tokens
import app_shared.security.jwt
import app_shared.security.rate_limit
import app_shared.url_safety
import app_shared.url_pattern
import app_shared.matches
import app_shared.matches.upsert
import app_shared.profiles
import app_shared.profiles.validation
import app_shared.profiles.confidence
import app_shared.profiles.resolution
import app_shared.profiles.repository
import app_shared.profiles.upsert
import app_shared.scrapyd
import app_shared.scrapyd.client
import app_shared.models.jobs
import app_shared.messaging
import app_shared.jobs
import app_shared.jobs.batching
import app_shared.jobs.nodes
import app_shared.jobs.lifecycle
import app_shared.jobs.targets
import app_shared.jobs.service

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

# SPEC-08 T049: the API jobs router imports the pure app_shared.jobs.service
# + app_shared.messaging seams to create jobs / enqueue dispatch work, but
# MUST NEVER import apps/workers (Constitution I — the API's import closure
# must never pull in the worker's).
_JOBS_ROUTER_IMPORT_CHECK = """
import sys

import app.routers.jobs as jobs_router
import app_shared.jobs.service
import app_shared.messaging

leaked = sorted(
    mod
    for mod in sys.modules
    if mod == "workers" or mod.startswith("workers.") or mod == "app.workers"
    or mod.startswith("app.workers.")
)
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)

sys.exit(0)
"""

_JOBS_ROUTER_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}


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

# Full check on the allowed direction: every scrape_core.* module built out
# in SPEC-07 (db seam, items, errors, validation, the extraction chain,
# fetch-time SSRF safety, robots, batched persistence pipeline) imports
# cleanly and pulls in app_shared alongside it. Unlike app_shared, these
# modules are allowed (expected, even) to also pull in scrapy/twisted — that
# is the scraping runtime scrape_core wraps — so this check only asserts the
# import succeeds and both packages land in sys.modules, never that
# scrapy/twisted are absent.
_SCRAPE_CORE_FULL_IMPORT_CHECK = """
import sys

import scrape_core
import scrape_core.db
import scrape_core.items
import scrape_core.errors
import scrape_core.validation
import scrape_core.extraction
import scrape_core.extraction.result
import scrape_core.extraction.jsonld
import scrape_core.extraction.css
import scrape_core.extraction.regex
import scrape_core.extraction.pipeline
import scrape_core.safety
import scrape_core.safety.fetch
import scrape_core.safety.resolver
import scrape_core.safety.middleware
import scrape_core.robots
import scrape_core.pipelines
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


def test_jobs_router_never_imports_apps_workers() -> None:
    """SPEC-08 T049: importing the jobs router never pulls in apps/workers."""
    import os

    env = {**os.environ, **_JOBS_ROUTER_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _JOBS_ROUTER_IMPORT_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, (
        "app.routers.jobs pulled in apps/workers:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    # Static check: the router source never references apps.workers/
    # app.workers at all, so the boundary can't be reintroduced later via a
    # lazy/deferred import the runtime check wouldn't catch.
    router_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "apps"
        / "api"
        / "app"
        / "routers"
        / "jobs.py"
    )
    contents = router_path.read_text(encoding="utf-8")
    assert "apps.workers" not in contents and "app.workers" not in contents, (
        f"{router_path} references apps/workers — forbidden dependency edge"
    )
    # Job creation (and, transitively through it, the app_shared.messaging
    # enqueue-by-name seam) is delegated to app_shared.jobs.service -- the
    # router itself never needs a direct app_shared.messaging import.
    assert "app_shared.jobs.service" in contents


def test_scrape_core_new_modules_import_cleanly_with_app_shared() -> None:
    """T050: every SPEC-07 scrape_core.* module (db, items, errors,
    validation, extraction/*, safety/*, robots, pipelines) imports without
    error and pulls in app_shared alongside it — the allowed one-way edge
    holds even once scrape_core is fully built out, not just at the
    package-marker stage covered by ``test_scrape_core_may_import_app_shared``.
    """
    result = _run_in_subprocess(_SCRAPE_CORE_FULL_IMPORT_CHECK)

    assert result.returncode == 0, (
        "a scrape_core.* module failed to import, or did not pull in "
        f"app_shared:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
