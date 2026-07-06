"""Import-boundary tests for the shared-library dependency direction (T043,
T045, T050, SPEC-08 T049, SPEC-09 T040).

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
* SPEC-09: ``app_shared.alerts.engine`` (and the ``alerts`` package) is
  a **pure** module — its own import statements pull in nothing beyond
  stdlib ``decimal`` + ``app_shared.enums`` (+ optional
  ``app_shared.money``); asserted via an **AST** parse of the source
  file's own ``import``/``from ... import`` statements (not a
  ``sys.modules`` runtime check — ``app_shared.enums`` itself
  legitimately imports sqlalchemy for ``enum_column``'s
  ``TypeDecorator``, so a transitive runtime check would false-positive;
  see contracts/alert-engine.md "Acceptance"). ``app_shared.models.alerts``
  (SPEC-09 T005) imports no scrapy/twisted/fastapi (sqlalchemy is
  expected — it is an ORM model module). ``apps/api/app/routers/alerts.py``
  + the SPEC-09 additions to ``routers/variants.py``/``routers/matches.py``
  import ``app_shared.messaging``, never ``apps.workers``.
  ``scrape_core.pipelines`` (trigger (a), SPEC-09 T029) imports
  ``app_shared.messaging``/``app_shared.redis_client``, never
  fastapi/apps.workers.
* SPEC-10 (T038): ``app_shared.models.access`` (the three new ORM models)
  imports no scrapy/twisted/fastapi (sqlalchemy is expected). The new
  ``app_shared.access`` package (``engine``/``resolution``/``repository``/
  ``budget``) and ``app_shared.security.encryption`` import no
  scrapy/twisted/fastapi and no ``apps.*``/``app.*`` — asserted both by the
  whole-package subprocess import check (extended below) and by a
  dedicated AST-based static check per module (mirroring the SPEC-09
  alerts-engine purity check), since ``engine.py``/``resolution.py`` are
  meant to be pure/stdlib-only while ``repository.py`` legitimately pulls
  in sqlalchemy.

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
import app_shared.models.access
import app_shared.pagination
import app_shared.catalog
import app_shared.repository
import app_shared.redis_client
import app_shared.security
import app_shared.security.passwords
import app_shared.security.tokens
import app_shared.security.jwt
import app_shared.security.rate_limit
import app_shared.security.encryption
import app_shared.access
import app_shared.access.engine
import app_shared.access.resolution
import app_shared.access.repository
import app_shared.access.budget
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


# --- SPEC-09 T040 ------------------------------------------------------------

# --- SPEC-14 T037: scrape_core.targets/result_builder import only
# app_shared.*/scrape_core.* (never Scrapy/Twisted/Playwright directly);
# scrape_core.browser.* is the one place allowed to import Scrapy/
# scrapy-playwright (contracts/shared-extraction.md "import_boundaries
# test stays green"). -----------------------------------------------------

_SCRAPE_CORE_SHARED_FORBIDDEN_ROOTS = frozenset(
    {"scrapy", "scrapy_playwright", "twisted", "playwright", "fastapi"}
)


def test_scrape_core_targets_and_result_builder_never_import_scrapy_stack() -> None:
    """SPEC-14 T037a: `scrape_core.targets`/`result_builder` — the
    transport-agnostic machinery shared by both the HTTP and browser
    spiders — never import Scrapy/scrapy-playwright/Twisted/Playwright/
    FastAPI directly. They may (and do) import sqlalchemy, stdlib, and
    other `app_shared.*`/`scrape_core.*` modules; only the Scrapy-stack
    roots are forbidden here."""
    import scrape_core.result_builder
    import scrape_core.targets

    for module in (scrape_core.targets, scrape_core.result_builder):
        source_file = pathlib.Path(module.__file__)
        roots = _imported_root_modules(source_file)
        leaked = roots & _SCRAPE_CORE_SHARED_FORBIDDEN_ROOTS
        assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


_SCRAPE_CORE_BROWSER_IMPORT_CHECK = """
import sys

import scrape_core
import scrape_core.browser
import scrape_core.browser.ssrf
import scrape_core.browser.variant
import scrape_core.browser.page
import app_shared

if "scrape_core" not in sys.modules or "app_shared" not in sys.modules:
    print("MISSING")
    sys.exit(1)

sys.exit(0)
"""


def test_scrape_core_browser_modules_import_cleanly_and_may_pull_in_playwright() -> None:
    """SPEC-14 T037a: `scrape_core.browser.*` (ssrf/variant/page) is the
    one place in `scrape_core` allowed to import Scrapy/scrapy-playwright
    (`page.py` imports `scrapy_playwright.page.PageMethod`) — this check
    only asserts the import succeeds and app_shared is reachable alongside
    it, mirroring `test_scrape_core_new_modules_import_cleanly_with_app_shared`
    for the SPEC-07 modules; it never asserts scrapy/playwright are absent."""
    result = _run_in_subprocess(_SCRAPE_CORE_BROWSER_IMPORT_CHECK)
    assert result.returncode == 0, (
        "scrape_core.browser.* failed to import, or did not pull in app_shared:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


_ALERTS_FORBIDDEN_ROOTS = frozenset({"sqlalchemy", "celery", "fastapi", "scrapy", "redis"})


def _imported_root_modules(source_path: pathlib.Path) -> set[str]:
    """AST-parse ``source_path`` and return the set of top-level root
    module names it imports (``import a.b.c`` / ``from a.b import c`` both
    contribute root ``a``). Static, not a runtime ``sys.modules`` check —
    see the module docstring for why that distinction matters here.
    """
    import ast

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def _alerts_package_dir() -> pathlib.Path:
    import app_shared.alerts

    return pathlib.Path(app_shared.alerts.__file__).parent


def test_alerts_engine_is_pure_no_forbidden_imports() -> None:
    """SPEC-09 T040 / contracts/alert-engine.md "Acceptance": the pure
    engine module's own import statements never name sqlalchemy, celery,
    fastapi, scrapy, or redis (stdlib decimal + app_shared.enums, plus
    optionally app_shared.money, only)."""
    package_dir = _alerts_package_dir()
    for source_file in sorted(package_dir.glob("*.py")):
        roots = _imported_root_modules(source_file)
        leaked = roots & _ALERTS_FORBIDDEN_ROOTS
        assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


def test_models_alerts_does_not_import_scrapy_twisted_fastapi() -> None:
    """SPEC-09 T005/T040: the alerts ORM model module is free to import
    sqlalchemy (it's an ORM module) but must never import scrapy/twisted/
    fastapi."""
    import app_shared.models.alerts

    source_file = pathlib.Path(app_shared.models.alerts.__file__)
    roots = _imported_root_modules(source_file)
    leaked = roots & {"scrapy", "twisted", "fastapi"}
    assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


# --- SPEC-10 T038 ------------------------------------------------------------

_ACCESS_FORBIDDEN_ROOTS = frozenset({"scrapy", "twisted", "fastapi", "apps", "app"})


def _access_package_dir() -> pathlib.Path:
    import app_shared.access

    return pathlib.Path(app_shared.access.__file__).parent


def test_access_package_no_forbidden_imports() -> None:
    """SPEC-10 T038: every module in the new ``app_shared.access`` package
    (``engine``/``resolution``/``repository``/``budget``) is free to import
    stdlib + sqlalchemy (``repository.py`` is a SQLAlchemy query helper) but
    must never import scrapy/twisted/fastapi or reach into ``apps.*``/
    ``app.*`` — the pure engines (``engine``/``resolution``/``budget``) stay
    framework-free and even the ORM-touching ``repository`` module never
    crosses into the apps/ side of the monorepo."""
    package_dir = _access_package_dir()
    for source_file in sorted(package_dir.glob("*.py")):
        roots = _imported_root_modules(source_file)
        leaked = roots & _ACCESS_FORBIDDEN_ROOTS
        assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


def test_encryption_module_no_forbidden_imports() -> None:
    """SPEC-10 T038: ``app_shared.security.encryption`` (the Fernet keyring)
    depends only on ``cryptography.fernet`` + ``app_shared.config`` — never
    scrapy/twisted/fastapi/apps.*."""
    import app_shared.security.encryption

    source_file = pathlib.Path(app_shared.security.encryption.__file__)
    roots = _imported_root_modules(source_file)
    leaked = roots & _ACCESS_FORBIDDEN_ROOTS
    assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


def test_models_access_does_not_import_scrapy_twisted_fastapi() -> None:
    """SPEC-10 T006/T038: the access ORM model module (ProxyProvider/
    AccessPolicy/DomainAccessRule) is free to import sqlalchemy (it's an ORM
    module) but must never import scrapy/twisted/fastapi."""
    import app_shared.models.access

    source_file = pathlib.Path(app_shared.models.access.__file__)
    roots = _imported_root_modules(source_file)
    leaked = roots & {"scrapy", "twisted", "fastapi"}
    assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


# --- SPEC-15 T035 ------------------------------------------------------------

_MAINTENANCE_PACKAGE_FORBIDDEN_ROOTS = frozenset(
    {"scrapy", "twisted", "playwright", "fastapi", "apps", "app"}
)


def _maintenance_package_dir() -> pathlib.Path:
    import app_shared.maintenance

    return pathlib.Path(app_shared.maintenance.__file__).parent


def test_maintenance_package_no_forbidden_imports() -> None:
    """SPEC-15 T035: every module in the new ``app_shared.maintenance``
    package (``registry``/``partitions``/``rollups``/``retention``/
    ``soft_refs``) is free to import stdlib + sqlalchemy (it is a
    DB-facing maintenance core) but must never import Scrapy/Twisted/
    Playwright (Constitution I/V, FR-003) — those belong only to the
    Scrapyd-side scraping stack — and, like every other ``app_shared``
    module, must never reach into ``apps.*``/``app.*`` (the reverse
    dependency edge is forbidden)."""
    package_dir = _maintenance_package_dir()
    for source_file in sorted(package_dir.glob("*.py")):
        roots = _imported_root_modules(source_file)
        leaked = roots & _MAINTENANCE_PACKAGE_FORBIDDEN_ROOTS
        assert not leaked, f"{source_file} imports forbidden module(s): {sorted(leaked)}"


def test_tasks_maintenance_no_scrapy_twisted_playwright() -> None:
    """SPEC-15 T035: the three maintenance Celery tasks
    (``partition_create``/``daily_rollup``/``retention_drop`` in
    ``apps/workers/app/workers/tasks_maintenance.py``) never import
    Scrapy/Twisted/Playwright directly — they are pure DB-maintenance
    tasks (DDL + rollup/retention aggregation on the BYPASSRLS system
    session), never a scraping runtime."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    tasks_maintenance_path = (
        repo_root / "apps" / "workers" / "app" / "workers" / "tasks_maintenance.py"
    )
    roots = _imported_root_modules(tasks_maintenance_path)
    leaked = roots & {"scrapy", "twisted", "playwright"}
    assert not leaked, f"{tasks_maintenance_path} imports forbidden module(s): {sorted(leaked)}"


_ALERTS_ROUTERS_IMPORT_CHECK = """
import sys

import app.routers.alerts as alerts_router
import app.routers.variants as variants_router
import app.routers.matches as matches_router
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


def test_alerts_routers_never_import_apps_workers() -> None:
    """SPEC-09 T040: the alerts router + the SPEC-09 additions to the
    variants/matches routers import app_shared.messaging to enqueue
    PRICE_ANALYSIS_RECOMPUTE by name, but MUST NEVER import apps/workers."""
    import os

    env = {**os.environ, **_JOBS_ROUTER_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _ALERTS_ROUTERS_IMPORT_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, (
        "app.routers.alerts/variants/matches pulled in apps/workers:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for relative in (
        "apps/api/app/routers/alerts.py",
        "apps/api/app/routers/variants.py",
        "apps/api/app/routers/matches.py",
    ):
        router_path = repo_root / relative
        contents = router_path.read_text(encoding="utf-8")
        assert "apps.workers" not in contents and "app.workers" not in contents, (
            f"{router_path} references apps/workers — forbidden dependency edge"
        )


_PIPELINES_NO_FASTAPI_NO_WORKERS_CHECK = """
import sys

import scrape_core.pipelines
import app_shared.messaging
import app_shared.redis_client

leaked = sorted(
    mod
    for mod in sys.modules
    if mod == "fastapi" or mod.startswith("fastapi.")
    or mod == "workers" or mod.startswith("workers.")
    or mod == "app.workers" or mod.startswith("app.workers.")
)
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)

sys.exit(0)
"""


def test_scrape_core_pipelines_never_imports_fastapi_or_apps_workers() -> None:
    """SPEC-09 T029/T040: the trigger (a) additions to scrape_core.pipelines
    (app_shared.redis_client for the SET NX dedup key, app_shared.messaging
    for the by-name enqueue) never pull in fastapi or apps/workers."""
    result = _run_in_subprocess(_PIPELINES_NO_FASTAPI_NO_WORKERS_CHECK)
    assert result.returncode == 0, (
        "scrape_core.pipelines pulled in fastapi or apps/workers:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    pipelines_path = repo_root / "libs" / "scrape-core" / "scrape_core" / "pipelines.py"
    # AST-based (not substring) -- the module's own docstring discusses
    # "apps.workers"/"fastapi" in prose, which a naive substring search
    # would false-positive on.
    roots = _imported_root_modules(pipelines_path)
    leaked = roots & {"app", "apps", "workers", "fastapi"}
    assert not leaked, f"{pipelines_path} imports forbidden module(s): {sorted(leaked)}"
