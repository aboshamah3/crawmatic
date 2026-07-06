"""Shared support for the SPEC-07 Phase 8 live-stack spider integration
tests (``test_spider_*_live.py``, ``test_dispatch_scrapyd_live.py``,
``test_observations_isolation_live.py``).

**Not a test module** — its filename deliberately does not match
pytest's default ``test_*.py`` collection pattern, so it is never
collected itself; it exists purely as an importable helper library for
its sibling live test files, mirroring the "each live test file owns
its own reachability probe" convention (`tests/integration/test_status_cache.py`,
`tests/integration/test_profile_resolution_live.py`, ...) while
avoiding five copies of the heavier machinery unique to this spec:

1. :func:`live_stack_reachable` — a best-effort Postgres(+tables)/Redis
   reachability probe (Scrapyd reachability is checked separately by
   :func:`live_scrapyd_reachable`, only needed by the dispatch test).
2. :func:`serve_fixture_pages` — a tiny loopback ``http.server`` handing
   back canned HTML bodies (and optional 302 redirects) so a live spider
   run never makes a real network call (FR-021/SC-007).
3. :func:`run_generic_price_spider_subprocess` — runs
   ``generic_price_spider`` via ``scrapy.crawler.CrawlerProcess`` in its
   **own OS process**. Twisted's reactor can only ever be started once
   per process (``ReactorNotRestartable``); a single ``pytest
   tests/integration -q`` invocation exercises several of these live
   spider tests in the *same* interpreter, so each crawl runs in a
   subprocess — exactly how Scrapyd itself launches one job per
   subprocess. This also gives each crawl an injectable ``DNS_RESOLVER``
   override: the production `apps/scrapers/price_monitor/settings.py`
   wires the real, allowlist-free
   ``scrape_core.safety.resolver.SafeResolver`` (correctly — there is no
   allowlist notion in production), so a test that needs its own
   loopback fixture server to be fetchable installs a **test-only**
   resolver subclass (source embedded directly in the generated runner
   script) that reuses ``app_shared.url_safety._reject_ip``/
   ``SafeResolver._reject_unsafe`` and only additionally allowlists the
   fixture server's own loopback IP — precisely the injectable
   resolver/allowlist seam ``contracts/fetch-url-safety.md`` / research
   D2 describes for happy-path fixture tests, just wired at the Scrapy
   ``DNS_RESOLVER`` layer instead of the pure-function layer
   `tests/unit/test_fetch_url_safety.py` already covers. Production
   wiring is never touched.
4. :func:`seed_workspace_with_variant`, :func:`seed_competitor`,
   :func:`seed_scrape_profile`, :func:`seed_match`,
   :func:`cleanup_seeded_workspace` — the ws/product/variant/competitor/
   match/profile seeding boilerplate every live spider test needs
   (US1/US3/US5 all seed "one workspace, one product+variant, one-or-more
   competitors+matches"), factored out once here rather than copied into
   each sibling file — mirrors the SPEC-05/06 live fixtures'
   ``get_session()``-direct-insert seeding convention (e.g.
   `tests/integration/test_profile_assignment_live.py`'s
   ``assignment_fixture``), just shared instead of duplicated because
   this spec's sibling live files need the identical shape four times
   over.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = [
    "live_stack_reachable",
    "live_scrapyd_reachable",
    "live_scrapyd_browser_reachable",
    "serve_fixture_pages",
    "run_generic_price_spider_subprocess",
    "SeededWorkspace",
    "seed_workspace_with_variant",
    "seed_competitor",
    "seed_scrape_profile",
    "seed_match",
    "cleanup_seeded_workspace",
]


# --- reachability probes ----------------------------------------------------


def live_stack_reachable(required_tables: Iterable[str] = ()) -> bool:
    """Best-effort probe: Postgres (with ``required_tables`` present) + Redis.

    Does **not** check Scrapyd — the spider-execution tests in this
    package run ``generic_price_spider`` directly (see
    :func:`run_generic_price_spider_subprocess`), not via a dispatched
    Scrapyd job, so Scrapyd reachability is irrelevant to them (only
    `test_dispatch_scrapyd_live.py` needs :func:`live_scrapyd_reachable`).
    """
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine
        from app_shared.redis_client import get_redis_client

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not set(required_tables) <= table_names:
            return False
        get_redis_client().ping()

        # Confirm the Scrapy project this test will subprocess into is
        # actually importable in this environment (it always is once
        # `uv sync --all-packages` has run, but stay defensive).
        import price_monitor.spiders.generic_price_spider  # noqa: F401
    except Exception:
        return False

    return True


def live_scrapyd_reachable() -> bool:
    """Best-effort probe: Redis + a live, authenticated Scrapyd node."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.REDIS_URL or not settings.SCRAPYD_HTTP_URLS:
        return False

    try:
        import requests

        from app_shared.redis_client import get_redis_client

        get_redis_client().ping()
        base = settings.SCRAPYD_HTTP_URLS[0].rstrip("/")
        resp = requests.get(
            f"{base}/daemonstatus.json",
            auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
            timeout=3,
        )
        if resp.status_code != 200:
            return False
    except Exception:
        return False

    return True


def live_scrapyd_browser_reachable() -> bool:
    """Best-effort probe: Redis + a live, authenticated Scrapyd *browser*
    node (SPEC-14, ``SCRAPYD_BROWSER_URLS``) -- mirrors
    :func:`live_scrapyd_reachable`, just against the separate browser
    node pool (SPEC-01's deployment scaffold; the browser-specific image/
    `scrapyd.conf` basic-auth are out of scope for this dispatch-routing
    fix, only its reachability is)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.REDIS_URL or not settings.SCRAPYD_BROWSER_URLS:
        return False

    try:
        import requests

        from app_shared.redis_client import get_redis_client

        get_redis_client().ping()
        base = settings.SCRAPYD_BROWSER_URLS[0].rstrip("/")
        resp = requests.get(
            f"{base}/daemonstatus.json",
            auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
            timeout=3,
        )
        if resp.status_code != 200:
            return False
    except Exception:
        return False

    return True


# --- loopback fixture HTTP server -------------------------------------------


def serve_fixture_pages(
    pages: Mapping[str, str], *, redirects: Mapping[str, str] | None = None
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    """Serve ``pages`` (``path -> html body``) on ``127.0.0.1`` (ephemeral port).

    ``redirects`` (``path -> Location``) answers with a bare ``302``
    instead of a body — used by the SSRF redirect-hop scenario. Any
    other path (including ``/robots.txt``) answers ``404``, which
    ``scrape_core.robots.RobotsPolicyMiddleware``'s default fetcher
    treats as "no robots.txt -> allow everything" (conventional
    semantics), so no fixture page needs to serve a robots.txt itself.

    Caller owns the returned server's lifecycle: always
    ``server.shutdown(); server.server_close()`` when done (a
    ``pytest`` fixture's teardown is the natural place).
    """
    redirects = dict(redirects or {})
    page_map = dict(pages)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.end_headers()
                return
            body = page_map.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep test output quiet -- no real request is interesting here

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_port


# --- run the spider as its own OS process -----------------------------------


_RUNNER_TEMPLATE = """
{resolver_source}
{commit_hook_source}
from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

import price_monitor.settings as _base_settings
from price_monitor.spiders.generic_price_spider import GenericPriceSpider

settings = Settings()
settings.setmodule(_base_settings, priority="project")
for _k, _v in {extra_settings!r}.items():
    settings.set(_k, _v, priority="cmdline")
{dns_resolver_line}

process = CrawlerProcess(settings, install_root_handler=False)
process.crawl(
    GenericPriceSpider,
    workspace_id={workspace_id!r},
    scrape_job_id={scrape_job_id!r},
    match_ids={match_ids_arg!r},
    mode="HTTP",
)
process.start()
"""


def run_generic_price_spider_subprocess(
    *,
    workspace_id: object,
    scrape_job_id: object,
    match_ids: Iterable[object],
    resolver_source: str = "",
    dns_resolver_dotted_path: str | None = None,
    extra_settings: Mapping[str, object] | None = None,
    commit_log_path: str | None = None,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``generic_price_spider`` end-to-end in a fresh subprocess.

    ``resolver_source`` is raw Python source (module-level, dedented)
    defining a ``_TestResolver`` class; pass ``dns_resolver_dotted_path``
    (typically ``"__main__._TestResolver"``, since the generated script
    runs as ``__main__``) to install it as the crawl's ``DNS_RESOLVER``.
    Omit both to use the real, unmodified production
    ``scrape_core.safety.resolver.SafeResolver``.

    ``commit_log_path``, if given, has one line appended to it (via a
    ``sqlalchemy`` ``"commit"`` engine event) for every DB transaction
    commit the crawl performs — proves the batched-flush commit count is
    ``≪`` the item count (US5/SC-006) without needing to smuggle a
    counter back out of a separate process any other way.

    Returns the completed subprocess (``returncode``/``stdout``/``stderr``)
    for the caller to assert on; never raises on a non-zero exit so the
    caller can surface ``stderr`` in the assertion message.
    """
    commit_hook_source = ""
    if commit_log_path:
        commit_hook_source = textwrap.dedent(
            f"""
            from sqlalchemy import event as _event
            from app_shared.database import get_engine as _get_engine

            def _on_commit(_conn):
                with open({commit_log_path!r}, "a") as _f:
                    _f.write("1\\n")

            _event.listen(_get_engine(), "commit", _on_commit)
            """
        )

    dns_resolver_line = ""
    if dns_resolver_dotted_path:
        dns_resolver_line = (
            f'settings.set("DNS_RESOLVER", {dns_resolver_dotted_path!r}, priority="cmdline")'
        )

    script = _RUNNER_TEMPLATE.format(
        resolver_source=textwrap.dedent(resolver_source),
        commit_hook_source=commit_hook_source,
        extra_settings=dict(extra_settings or {}),
        dns_resolver_line=dns_resolver_line,
        workspace_id=str(workspace_id),
        scrape_job_id=str(scrape_job_id),
        match_ids_arg=",".join(str(m) for m in match_ids),
    )

    fd, script_path = tempfile.mkstemp(suffix="_generic_price_spider_runner.py")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(script)
        return subprocess.run(  # noqa: S603 - fixed interpreter, generated script, test-only
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    finally:
        os.unlink(script_path)


# --- ws/product/variant/competitor/match/profile seeding --------------------


@dataclass
class SeededWorkspace:
    """One seeded workspace + one product + one product variant."""

    workspace_id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    _competitor_ids: list[uuid.UUID] = field(default_factory=list)
    _profile_ids: list[uuid.UUID] = field(default_factory=list)
    _match_ids: list[uuid.UUID] = field(default_factory=list)


def seed_workspace_with_variant(name_prefix: str) -> SeededWorkspace:
    """Seed one workspace + one product + one product variant.

    Direct-insert via ``app_shared.database.get_session()`` — the same
    seeding convention every prior spec's live fixture uses (e.g.
    `tests/integration/test_profile_assignment_live.py`'s
    ``assignment_fixture``, `tests/integration/test_workspace_isolation_live.py`'s
    ``two_workspaces_with_products``).
    """
    from decimal import Decimal

    from app_shared.database import get_session
    from app_shared.enums import ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.catalog import Product, ProductVariant

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        workspace = Workspace(
            name=f"{name_prefix} {unique}",
            slug=f"{name_prefix}-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        product = Product(
            workspace_id=workspace.id,
            title=f"{name_prefix} product {unique}",
            status=ProductStatus.ACTIVE,
        )
        session.add(product)
        session.flush()

        variant = ProductVariant(
            workspace_id=workspace.id,
            product_id=product.id,
            title="Default",
            current_price=Decimal("0.0000"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add(variant)
        session.flush()
        session.commit()

        return SeededWorkspace(
            workspace_id=workspace.id,
            product_id=product.id,
            product_variant_id=variant.id,
        )


def seed_competitor(
    seeded: SeededWorkspace,
    name: str,
    *,
    robots_policy: Any = None,
) -> uuid.UUID:
    """Seed one ``competitors`` row in ``seeded.workspace_id``; returns its id."""
    from app_shared.database import get_session
    from app_shared.enums import RobotsPolicy
    from app_shared.models.competitors_matches import Competitor

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        competitor = Competitor(
            workspace_id=seeded.workspace_id,
            name=f"{name} {unique}",
            domain=f"{name}-{unique}.invalid",
            robots_policy=robots_policy or RobotsPolicy.IGNORE_AFTER_APPROVAL,
        )
        session.add(competitor)
        session.commit()
        seeded._competitor_ids.append(competitor.id)
        return competitor.id


def seed_scrape_profile(seeded: SeededWorkspace, name: str, **profile_kwargs: Any) -> uuid.UUID:
    """Seed one workspace-owned ``scrape_profiles`` row; returns its id."""
    from app_shared.database import get_session
    from app_shared.models.scrape_profiles import ScrapeProfile

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        profile = ScrapeProfile(
            workspace_id=seeded.workspace_id,
            name=f"{name}-{unique}",
            **profile_kwargs,
        )
        session.add(profile)
        session.commit()
        seeded._profile_ids.append(profile.id)
        return profile.id


def seed_match(
    seeded: SeededWorkspace,
    competitor_id: uuid.UUID,
    url: str,
    *,
    scrape_profile_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Seed one ``competitor_product_matches`` row linking the seeded variant
    to ``competitor_id``/``url``; returns its id."""
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import CompetitorProductMatch

    with get_session() as session:
        match = CompetitorProductMatch(
            workspace_id=seeded.workspace_id,
            product_id=seeded.product_id,
            product_variant_id=seeded.product_variant_id,
            competitor_id=competitor_id,
            competitor_url=url,
            normalized_competitor_url=url,
            url_pattern=url,
            url_pattern_version=1,
            scrape_profile_id=scrape_profile_id,
        )
        session.add(match)
        session.commit()
        seeded._match_ids.append(match.id)
        return match.id


def cleanup_seeded_workspace(seeded: SeededWorkspace) -> None:
    """Delete every row this module's seeding helpers created for ``seeded``,
    deepest-dependent first, including the SPEC-07 observation tables."""
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM match_current_prices WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM request_attempts WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM price_observations WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM product_variants WHERE workspace_id = :ws"),
            {"ws": seeded.workspace_id},
        )
        session.execute(
            text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": seeded.workspace_id}
        )
        for profile_id in seeded._profile_ids:
            session.execute(text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": profile_id})
        session.execute(
            text("DELETE FROM workspaces WHERE id = :ws"), {"ws": seeded.workspace_id}
        )
        session.commit()
