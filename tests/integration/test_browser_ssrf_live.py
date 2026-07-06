"""Live browser-spider SSRF-refusal test (SPEC-14 T034, US4 AS2, SC-005,
`contracts/browser-safety.md` "SSRF") — DEFERRED.

Two scenarios, both proving the per-navigation-hop resolved-IP guard
(``PLAYWRIGHT_ABORT_REQUEST = scrape_core.browser.ssrf.abort_unsafe_request``)
refuses a browser navigation **before any page body is processed**, with
the target recorded as exactly one ``BLOCKED`` ``request_attempts`` row and
no observation ever persisted:

1. A match whose URL host is a literal private-range IP -- rejected by the
   reused save-time IP-literal deny check (`app_shared.url_safety`) inside
   `abort_unsafe_request`'s per-hop resolved-IP re-validation.
2. A match whose host is *not* an IP literal (so the pre-fetch
   ``SsrfGuardMiddleware`` scheme/userinfo guard alone lets it through) but
   resolves via the real system resolver to a loopback address
   (``localhost``) -- proving `abort_unsafe_request`'s own resolved-IP
   check (not merely the pre-fetch layer) is what catches it; this is the
   gap ``PLAYWRIGHT_ABORT_REQUEST`` specifically exists to close, since
   ``scrapy-playwright`` never consults Scrapy's ``DNS_RESOLVER`` and
   Chromium follows any redirect internally (bypassing
   ``RedirectMiddleware``/``SsrfGuardMiddleware`` for that hop) -- see
   `scrape_core.browser.ssrf` module docstring. The second match's fixture
   additionally 302s from that loopback host to a second, distinct
   private-IP-literal address, exercising the "re-runs on every redirect
   hop" guarantee end to end.

Zero real-competitor network calls (FR-021/SC-007) -- every host below is
either the loopback fixture server's own address or a non-routable
private-range literal that is never actually dialed (`abort_unsafe_request`
refuses the navigation before Chromium ever opens a connection).

Needs a reachable Postgres (SPEC-07 migration applied) + Redis AND an
installed Chromium binary (``playwright install``) -- this no-container-
engine build environment has neither, so this SKIPS cleanly here (never
faked).

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
an installed Chromium binary).
"""

from __future__ import annotations

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

_SUBPROCESS_TIMEOUT_SECONDS = 30.0
# Non-routable private-range literal (RFC 5737-adjacent private space) --
# never actually dialed, `abort_unsafe_request` refuses the navigation
# before any connection attempt.
_PRIVATE_IP_LITERAL = "10.255.255.1"
_REDIRECT_TARGET_PRIVATE_IP = "10.255.255.2"

_MARKER_HTML = "<html><body>should never be served</body></html>"


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec14-browser-ssrf")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def _fetch_attempts(workspace_id: uuid.UUID, match_id: uuid.UUID) -> list[object]:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        return session.execute(
            text(
                "SELECT access_method, success, error_code "
                "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": workspace_id, "match": match_id},
        ).fetchall()


def _fetch_observations(workspace_id: uuid.UUID, match_id: uuid.UUID) -> list[object]:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        return session.execute(
            text(
                "SELECT id FROM price_observations "
                "WHERE workspace_id = :ws AND match_id = :match"
            ),
            {"ws": workspace_id, "match": match_id},
        ).fetchall()


def test_private_ip_literal_host_is_refused_before_body_with_one_blocked_attempt(
    seeded: SeededWorkspace,
) -> None:
    competitor_id = seed_competitor(seeded, "browser-ssrf-competitor")
    match_url = f"http://{_PRIVATE_IP_LITERAL}/product"
    match_id = seed_match(seeded, competitor_id, match_url)
    scrape_job_id = uuid.uuid4()

    result = run_generic_browser_price_spider_subprocess(
        workspace_id=seeded.workspace_id,
        scrape_job_id=scrape_job_id,
        match_ids=[match_id],
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr

    assert _fetch_observations(seeded.workspace_id, match_id) == []

    attempts = _fetch_attempts(seeded.workspace_id, match_id)
    assert len(attempts) == 1, attempts
    assert attempts[0].success is False
    assert attempts[0].error_code == "BLOCKED"


def test_hostname_resolving_to_loopback_and_redirect_to_internal_are_refused_pre_body(
    seeded: SeededWorkspace,
) -> None:
    """A non-IP-literal host (`localhost`) that resolves to loopback via the
    real system resolver, whose fixture page 302s to a second, distinct
    private-IP-literal -- both hops refused by `abort_unsafe_request`
    (never `SsrfGuardMiddleware` alone, which only ever sees the original
    Scrapy request, not a Chromium-internal redirect hop)."""
    server, thread, port = serve_fixture_pages(
        {"/redirect-target-marker": _MARKER_HTML},
        redirects={"/start": f"http://{_REDIRECT_TARGET_PRIVATE_IP}/redirect-target-marker"},
    )
    try:
        competitor_id = seed_competitor(seeded, "browser-ssrf-redirect-competitor")
        match_url = f"http://localhost:{port}/start"
        match_id = seed_match(seeded, competitor_id, match_url)
        scrape_job_id = uuid.uuid4()

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, result.stderr

        assert _fetch_observations(seeded.workspace_id, match_id) == []

        attempts = _fetch_attempts(seeded.workspace_id, match_id)
        assert len(attempts) == 1, attempts
        assert attempts[0].success is False
        assert attempts[0].error_code == "BLOCKED"
    finally:
        server.shutdown()
        server.server_close()
