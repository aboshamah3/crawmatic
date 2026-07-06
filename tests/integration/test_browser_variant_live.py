"""Live end-to-end variant-selection browser spider run (SPEC-14 T029,
US3 AS1/AS2/AS3, SC-003, `contracts/variant-selection.md`).

Runs the real ``generic_browser_price_spider`` (via
``run_generic_browser_price_spider_subprocess``, exactly the T019/T020
rationale) against a fixture page whose displayed price only changes
*after* a variant is selected (a ``<select>`` that swaps a
``<div id="price">`` via inline JS ``onchange``):

1. A profile with ``variant_selector_config`` (``select_option`` on the
   size selector, ``value_from: "options.size"``, then a ``settle``
   wait on the price node) and a match whose
   ``competitor_variant_options={"size": "L"}`` -- asserts the
   *post-selection* price is what gets persisted (US3 AS1).
2. The same fixture page, but a plain match with **no**
   ``variant_selector_config`` on its profile -- asserts the *default*
   (pre-selection) price is what gets persisted, no interaction attempted
   (US3 AS2, FR-004).
3. A profile whose ``variant_selector_config`` references a selector that
   is never present on the page (a variant that doesn't exist there) --
   asserts a terminal ``VARIANT_NOT_FOUND`` failed attempt, no price
   persisted (US3 AS3).

Needs a reachable Postgres (SPEC-07 migration applied) + Redis (same
probe as the HTTP/US1 live suites) AND an installed Chromium binary
(``playwright install``) -- this no-container-engine build environment
has neither, so this SKIPS cleanly here (never faked).
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

# A `<select>` that swaps the displayed price via inline JS `onchange` --
# no server round trip, so this is a pure client-side variant-selection
# fixture (mirrors real "select a size, the price updates" product pages).
_VARIANT_PRICE_HTML = """
<html>
  <head><title>Variant product</title></head>
  <body>
    <select id="size" onchange="
      document.getElementById('price').textContent =
        this.value === 'L' ? '$79.99' : '$59.99';
      document.getElementById('price').setAttribute('data-ready', '1');
    ">
      <option value="M">Medium</option>
      <option value="L">Large</option>
    </select>
    <div id="price" data-ready="1">$59.99</div>
  </body>
</html>
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec14-browser-variant")
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


def _assert_single_failed_attempt(
    *, workspace_id: uuid.UUID, match_id: uuid.UUID, expected_error_code: str
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        attempts = session.execute(
            text(
                "SELECT success, error_code "
                "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": str(workspace_id), "match": str(match_id)},
        ).fetchall()
        assert len(attempts) == 1, attempts
        assert attempts[0].success is False
        assert attempts[0].error_code == expected_error_code

        observations = session.execute(
            text(
                "SELECT price FROM price_observations "
                "WHERE workspace_id = :ws AND match_id = :match AND success IS TRUE"
            ),
            {"ws": str(workspace_id), "match": str(match_id)},
        ).fetchall()
        assert observations == []


def test_variant_config_selects_size_and_persists_post_selection_price(
    seeded: SeededWorkspace,
) -> None:
    """US3 AS1: `variant_selector_config` selects size=L -- the persisted
    price is the post-selection ($79.99), not the page's default ($59.99)."""
    server, thread, port = serve_fixture_pages({"/product": _VARIANT_PRICE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-variant-competitor")
        profile_id = seed_scrape_profile(
            seeded,
            "browser-variant-profile",
            browser_timeout_ms=15000,
            variant_selector_config={
                "version": 1,
                "actions": [
                    {"type": "select_option", "selector": "#size", "value_from": "options.size"},
                ],
                "settle": {"wait_for_selector": "#price[data-ready='1']"},
            },
        )
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(
            seeded,
            competitor_id,
            match_url,
            scrape_profile_id=profile_id,
            competitor_variant_options={"size": "L"},
        )
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
            expected_price=Decimal("79.99"),
            scrape_job_id=scrape_job_id,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_no_variant_config_yields_default_price(seeded: SeededWorkspace) -> None:
    """US3 AS2: no `variant_selector_config` on the profile -- no
    interaction is attempted, the page's default price ($59.99) is
    extracted as-is (FR-004)."""
    server, thread, port = serve_fixture_pages({"/product": _VARIANT_PRICE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-no-variant-competitor")
        profile_id = seed_scrape_profile(
            seeded, "browser-no-variant-profile", browser_timeout_ms=15000
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
            expected_price=Decimal("59.99"),
            scrape_job_id=scrape_job_id,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_variant_target_missing_yields_variant_not_found(seeded: SeededWorkspace) -> None:
    """US3 AS3: `variant_selector_config` addresses a selector that is
    never present on the page (a variant option that doesn't exist there)
    -- a terminal `VARIANT_NOT_FOUND` failed attempt, no price persisted."""
    server, thread, port = serve_fixture_pages({"/product": _VARIANT_PRICE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-missing-variant-competitor")
        profile_id = seed_scrape_profile(
            seeded,
            "browser-missing-variant-profile",
            browser_timeout_ms=5000,
            variant_selector_config={
                "version": 1,
                "actions": [
                    {"type": "select_option", "selector": "#size", "value_from": "options.size"},
                ],
            },
        )
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(
            seeded,
            competitor_id,
            match_url,
            scrape_profile_id=profile_id,
            # "XL" is never an option on this fixture page -- the
            # select_option locator never finds a matching option, so
            # Playwright's own timeout/element error surfaces here.
            competitor_variant_options={"size": "XL"},
        )
        scrape_job_id = uuid.uuid4()

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
        )
        assert result.returncode == 0, result.stderr

        _assert_single_failed_attempt(
            workspace_id=seeded.workspace_id,
            match_id=match_id,
            expected_error_code="VARIANT_NOT_FOUND",
        )
    finally:
        server.shutdown()
        server.server_close()
