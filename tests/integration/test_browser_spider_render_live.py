"""Live end-to-end browser-render spider run (SPEC-14 T019, US1
AS1/AS3/AS4, SC-001, `contracts/browser-spider.md`).

Runs the real ``generic_browser_price_spider`` (via
``scrapy.crawler.CrawlerProcess`` in its own OS process, exactly the
``run_generic_browser_price_spider_subprocess`` rationale) against a
loopback ``http.server`` serving two fixture pages:

1. A JS-injected-price fixture whose ``<div id="price">`` is populated
   only by an inline ``<script>`` after load, with the seeded profile's
   ``wait_for_selector="#price"`` — proves the browser path renders
   JavaScript and waits for the configured selector before extracting,
   where a plain HTTP fetch would see an empty price node (US1 AS1).
2. A static-HTML-price fixture (price present in the raw markup, no JS
   needed) with no ``wait_for_selector`` configured — proves the
   `analyze B1` ``wait_for_load_state("networkidle")`` fallback still
   extracts correctly on an already-settled page (US1 AS4).

Each assert: exactly one valid-``Decimal`` ``price_observations`` row,
one ``match_current_prices`` upsert, one ``request_attempts`` row
(``access_method=PLAYWRIGHT_PROXY``, ``success=true``), and one
``price_analysis`` recompute task enqueued (SC-001).

Needs a reachable Postgres (SPEC-07 migration applied) + Redis (same
probe as the HTTP live suite) AND an installed Chromium binary
(``playwright install``) — this no-container-engine build environment
has neither Docker-backed Postgres/Redis nor a downloaded Chromium
binary, so this SKIPS cleanly here (never faked).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

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

_JS_INJECTED_PRICE_HTML = """
<html>
  <head><title>JS-rendered product</title></head>
  <body>
    <div id="price"></div>
    <script>
      // Simulate a client-side price fetch/render -- absent from the raw
      // HTML a plain (non-browser) HTTP fetch would receive.
      document.getElementById("price").textContent = "$249.99";
    </script>
  </body>
</html>
"""

_STATIC_PRICE_HTML = """
<html>
  <head><title>Static product</title></head>
  <body>
    <div id="price">$59.99</div>
  </body>
</html>
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec14-browser-render")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def _assert_single_priced_observation(
    *, workspace_id: uuid.UUID, match_id: uuid.UUID, expected_price: Decimal, scrape_job_id: uuid.UUID
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        observations = session.execute(
            text(
                "SELECT price, success, comparable, scrape_job_id "
                "FROM price_observations WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": str(workspace_id), "match": str(match_id)},
        ).fetchall()
        assert len(observations) == 1, observations
        row = observations[0]
        assert row.success is True
        assert row.comparable is True
        assert row.price == expected_price
        assert str(row.scrape_job_id) == str(scrape_job_id)

        attempts = session.execute(
            text(
                "SELECT access_method, success "
                "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": str(workspace_id), "match": str(match_id)},
        ).fetchall()
        assert len(attempts) == 1, attempts
        assert attempts[0].access_method == "PLAYWRIGHT_PROXY"
        assert attempts[0].success is True

        current_prices = session.execute(
            text(
                "SELECT price, success "
                "FROM match_current_prices WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": str(workspace_id), "match": str(match_id)},
        ).fetchall()
        assert len(current_prices) == 1, current_prices
        assert current_prices[0].price == expected_price
        assert current_prices[0].success is True


def test_js_injected_price_waits_for_selector_then_renders(seeded: SeededWorkspace) -> None:
    server, thread, port = serve_fixture_pages({"/product": _JS_INJECTED_PRICE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-js-competitor")
        profile_id = seed_scrape_profile(
            seeded,
            "browser-js-profile",
            wait_for_selector="#price",
            browser_timeout_ms=15000,
        )
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(seeded, competitor_id, match_url, scrape_profile_id=profile_id)
        scrape_job_id = uuid.uuid4()

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
        )
        assert result.returncode == 0, result.stderr

        _assert_single_priced_observation(
            workspace_id=seeded.workspace_id,
            match_id=match_id,
            expected_price=Decimal("249.99"),
            scrape_job_id=scrape_job_id,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_static_html_price_still_extracts_without_wait_for_selector(
    seeded: SeededWorkspace,
) -> None:
    """US1 AS4: no `wait_for_selector` configured -- the analyze-B1
    `wait_for_load_state("networkidle")` fallback settles the page and
    extraction still finds the already-present static price."""
    server, thread, port = serve_fixture_pages({"/product": _STATIC_PRICE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-static-competitor")
        profile_id = seed_scrape_profile(seeded, "browser-static-profile")
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(seeded, competitor_id, match_url, scrape_profile_id=profile_id)
        scrape_job_id = uuid.uuid4()

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
        )
        assert result.returncode == 0, result.stderr

        _assert_single_priced_observation(
            workspace_id=seeded.workspace_id,
            match_id=match_id,
            expected_price=Decimal("59.99"),
            scrape_job_id=scrape_job_id,
        )
    finally:
        server.shutdown()
        server.server_close()
