"""Live fetch-time SSRF-refusal spider run (SPEC-07 US2 T045, FR-005..FR-006,
Principle VI, SC-002) — ⏸ DEFERRED.

Runs the real ``generic_price_spider`` against two unsafe targets in one
crawl (via ``run_generic_price_spider_subprocess`` — see that module's
docstring for why each live spider test runs in its own OS process) and
proves both enforcement points in ``contracts/fetch-url-safety.md`` refuse
the fetch **before any body download**, so neither target ever produces a
``success=true`` observation (US2/SC-002). Zero real-competitor network
calls — every host below is either a fully-invented ``.invalid`` name or
the loopback fixture server's own address (FR-021/SC-007).

Two distinct enforcement layers, each proven by its own match:

1. **Connect-time resolver defense** (``scrape_core.safety.resolver.SafeResolver``,
   installed via ``DNS_RESOLVER``) — match 1's URL names a host
   (``private-target.invalid``) that the test-only resolver maps to a
   private IP (``10.0.0.5``); ``SafeResolver`` refuses to hand back an
   unsafe resolved address, so the connection is never attempted. Twisted's
   ``HostnameEndpoint``/``SimpleResolverComplexifier`` machinery
   unconditionally discards that refusal (class, ``error_code``, and any
   cause chain alike) before it reaches the spider's ``errback``,
   replacing it with a generic ``scrapy.exceptions.CannotResolveHostError``
   indistinguishable — by type or message — from a genuine DNS miss
   (verified empirically while authoring the SPEC-07 tasks.md T053 fix).
   ``scrape_core.errors.classify_exception`` now recognizes this specific
   rejection via ``scrape_core.safety.rejection_registry`` — the
   short-TTL, hostname-keyed side-channel ``SafeResolver`` populates right
   before raising, consulted here using the failed request's own hostname
   — so it classifies ``BLOCKED`` (FR-005/US2 Acceptance Scenario 1)
   while a genuine DNS failure for an unrelated hostname still classifies
   ``DNS_ERROR``. This test proves both the non-negotiable guarantee (the
   connection to the private IP never happens, no observation is ever
   `success=true` for that target) and the correct classification.
2. **Pre-fetch middleware defense on a redirect hop**
   (``scrape_core.safety.middleware.SsrfGuardMiddleware``) — match 2's URL
   is served by the loopback fixture server at a "public-looking" host
   (``public-origin.invalid``, resolver-allowlisted to the fixture
   server's own loopback IP) which responds ``302`` with a ``Location``
   pointing at the fixture server's own address **as a literal
   ``127.0.0.1`` IP** (serving `ssrf_redirect_target.html`'s
   REDIRECT-MARKER body, were it ever reached). Scrapy's
   ``RedirectMiddleware`` re-emits that ``Location`` as a new request,
   which passes back through ``SsrfGuardMiddleware.process_request`` ->
   ``validate_competitor_url`` -> the IP-literal deny rule rejects it
   (``PRIVATE_OR_INTERNAL_IP``) **before any connection is attempted** --
   `SsrfRejectedError` explicitly carries `error_code=BLOCKED`, so this
   path *is* guaranteed to classify as `BLOCKED` (also verified
   empirically). The REDIRECT-MARKER body is therefore never served to
   the spider.

Needs a reachable Postgres (``DATABASE_URL``) with the SPEC-07 migration
applied AND a reachable Redis (``REDIS_URL``). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever either isn't usable or the required tables don't exist.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-07 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
    run_generic_price_spider_subprocess,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"
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
    not live_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-07 observations "
        "migration applied -- not available in this environment."
    ),
)

_PRIVATE_IP_HOST = "private-target.invalid"
_PUBLIC_ORIGIN_HOST = "public-origin.invalid"

# Resolver source for the generated runner script:
# - `private-target.invalid` -> forced-unsafe "10.0.0.5" (never a real DNS
#   lookup; SafeResolver's own reject logic decides this is unsafe, exactly
#   as it would for any real private-IP DNS answer).
# - `public-origin.invalid` -> allowlisted straight to the loopback fixture
#   server (the "safe" first hop, matching the "public URL redirecting to
#   an internal IP" scenario -- the redirect's target IS the SSRF, not the
#   first hop).
# - anything else falls through to the real (production) SafeResolver.
_RESOLVER_SOURCE = """
from twisted.internet import defer

from scrape_core.safety.resolver import SafeResolver


class _TestResolver(SafeResolver):
    def getHostByName(self, name, timeout=()):
        if name == "private-target.invalid":
            # Route through the real `_reject_unsafe` (not a re-implemented
            # reject check) so this exercises the exact production path --
            # including the `rejection_registry.mark_rejected` side-channel
            # `classify_exception` relies on (SPEC-07 tasks.md T053).
            return defer.succeed("10.0.0.5").addCallback(self._reject_unsafe, name)
        if name == "public-origin.invalid":
            return defer.succeed("127.0.0.1")
        return super().getHostByName(name, timeout)
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec07-ssrf")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def test_private_ip_and_redirect_to_internal_are_both_refused_pre_body(
    seeded: SeededWorkspace,
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    marker_html = (_FIXTURES_DIR / "ssrf_redirect_target.html").read_text(encoding="utf-8")

    # `serve_fixture_pages`'s `redirects` mapping needs the Location to be
    # a fixed string, but the redirect target here is deliberately the
    # server's *own* loopback address+port (an IP literal, not a hostname)
    # -- which isn't known until the ephemeral port is bound. Build the
    # handler directly (same shape as `serve_fixture_pages`) so the
    # Location can reference `self.server.server_port` once bound.
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/start":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/redirect-target-marker"
                )
                self.end_headers()
                return
            if self.path == "/redirect-target-marker":
                body = marker_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port

    try:
        competitor_id = seed_competitor(seeded, "ssrf-fixture-competitor")
        private_ip_url = f"http://{_PRIVATE_IP_HOST}/product"
        redirect_url = f"http://{_PUBLIC_ORIGIN_HOST}:{port}/start"
        private_ip_match_id = seed_match(seeded, competitor_id, private_ip_url)
        redirect_match_id = seed_match(seeded, competitor_id, redirect_url)
        scrape_job_id = uuid.uuid4()

        result = run_generic_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[private_ip_match_id, redirect_match_id],
            resolver_source=_RESOLVER_SOURCE,
            dns_resolver_dotted_path="__main__._TestResolver",
        )
        assert result.returncode == 0, result.stderr

        with get_session() as session:
            # No success=true observation was ever recorded for either
            # target -- the single non-negotiable SC-002 guarantee.
            success_rows = session.execute(
                text(
                    "SELECT match_id FROM price_observations "
                    "WHERE workspace_id = :ws AND match_id IN (:m1, :m2) AND success = true"
                ),
                {
                    "ws": str(seeded.workspace_id),
                    "m1": str(private_ip_match_id),
                    "m2": str(redirect_match_id),
                },
            ).fetchall()
            assert success_rows == []

            # Exactly one request_attempt per target, both success=false.
            for match_id in (private_ip_match_id, redirect_match_id):
                attempts = session.execute(
                    text(
                        "SELECT success, error_code FROM request_attempts "
                        "WHERE workspace_id = :ws AND match_id = :match"
                    ),
                    {"ws": str(seeded.workspace_id), "match": str(match_id)},
                ).fetchall()
                assert len(attempts) == 1, (match_id, attempts)
                assert attempts[0].success is False

            redirect_attempt = session.execute(
                text(
                    "SELECT error_code FROM request_attempts "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(redirect_match_id)},
            ).fetchone()
            # The redirect hop is refused by SsrfGuardMiddleware's IP-literal
            # deny rule, which explicitly sets error_code=BLOCKED.
            assert redirect_attempt.error_code == "BLOCKED"

            private_ip_attempt = session.execute(
                text(
                    "SELECT error_code FROM request_attempts "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(private_ip_match_id)},
            ).fetchone()
            # SPEC-07 tasks.md T053: the connect-time SafeResolver rejection
            # now also classifies as BLOCKED (FR-005/US2 Acceptance Scenario
            # 1) -- previously UNKNOWN_ERROR, see module docstring.
            assert private_ip_attempt.error_code == "BLOCKED"

            # No match_current_prices row was ever created for either target
            # (a failure/rejection never upserts the current price, FR-014).
            current_prices = session.execute(
                text(
                    "SELECT match_id FROM match_current_prices "
                    "WHERE workspace_id = :ws AND match_id IN (:m1, :m2)"
                ),
                {
                    "ws": str(seeded.workspace_id),
                    "m1": str(private_ip_match_id),
                    "m2": str(redirect_match_id),
                },
            ).fetchall()
            assert current_prices == []
    finally:
        server.shutdown()
        server.server_close()
