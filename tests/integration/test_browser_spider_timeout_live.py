"""Live browser-spider timeout test (SPEC-14 T020, US1 AS2, Edge Cases,
`contracts/browser-spider.md` errback).

Seeds a profile whose ``wait_for_selector`` targets an element the
fixture page never renders, bounded by a small ``browser_timeout_ms`` --
the ``wait_for_selector`` `PageMethod` raises Playwright's own
``TimeoutError``, which fails the request (not `parse`) and reaches
``errback``. Asserts: exactly one **failed** ``request_attempts`` row
(``error_code=TIMEOUT``, ``success=false``), no
``price_observations``/``match_current_prices`` row, the run completes
promptly (bounded -- no hang, R4 single-attempt no-retry), and no
Chromium page/context leaks past the run (the process exits cleanly).

Needs a reachable Postgres (SPEC-07 migration applied) + Redis AND an
installed Chromium binary (``playwright install``) -- this
no-container-engine build environment has neither, so this SKIPS
cleanly here (never faked).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest

from ._browser_spider_live_support import (
    live_browser_stack_reachable,
    run_generic_browser_price_spider_subprocess,
)
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_scrape_profile,
    seed_workspace_with_variant,
    serve_fixture_pages,
)

_REQUIRED_TABLES = frozenset(
    {
        "price_observations",
        "request_attempts",
        "match_current_prices",
        "competitor_product_matches",
        "competitors",
        "product_variants",
        "products",
        "workspaces",
        "scrape_profiles",
    }
)

pytestmark = pytest.mark.skipif(
    not live_browser_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-07 observations "
        "migration applied, AND an installed Playwright Chromium binary -- "
        "not available in this environment."
    ),
)

# No element with this id is ever rendered by the fixture page below --
# `wait_for_selector` must time out.
_NEVER_APPEARS_HTML = """
<html>
  <head><title>Never-rendered selector</title></head>
  <body>
    <div id="price">$19.99</div>
  </body>
</html>
"""

_SMALL_BROWSER_TIMEOUT_MS = 1000
# Generous wall-clock ceiling for the whole subprocess run -- proves the
# spider actually completes (bounded, no hang) rather than merely that
# the configured Playwright timeout value was small.
_SUBPROCESS_TIMEOUT_SECONDS = 30.0


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec14-browser-timeout")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def test_wait_for_selector_timeout_yields_one_failed_timeout_attempt(
    seeded: SeededWorkspace,
) -> None:
    server, thread, port = serve_fixture_pages({"/product": _NEVER_APPEARS_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-timeout-competitor")
        profile_id = seed_scrape_profile(
            seeded,
            "browser-timeout-profile",
            wait_for_selector="#selector-that-never-appears",
            browser_timeout_ms=_SMALL_BROWSER_TIMEOUT_MS,
        )
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(seeded, competitor_id, match_url, scrape_profile_id=profile_id)
        scrape_job_id = uuid.uuid4()

        started = time.monotonic()
        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        # Bounded: completes well within the generous subprocess ceiling
        # -- no hang, no leaked page holding the process open.
        assert elapsed < _SUBPROCESS_TIMEOUT_SECONDS

        from sqlalchemy import text

        from app_shared.database import get_session

        with get_session() as session:
            observations = session.execute(
                text(
                    "SELECT id FROM price_observations "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert observations == []

            current_prices = session.execute(
                text(
                    "SELECT id FROM match_current_prices "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert current_prices == []

            attempts = session.execute(
                text(
                    "SELECT access_method, success, error_code "
                    "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert len(attempts) == 1, attempts
            assert attempts[0].access_method == "PLAYWRIGHT_PROXY"
            assert attempts[0].success is False
            assert attempts[0].error_code == "TIMEOUT"
    finally:
        server.shutdown()
        server.server_close()
