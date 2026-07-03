"""Live end-to-end JSON-LD fixture spider run (SPEC-07 US1 T044, FR-001..FR-004/
FR-020, SC-001) — ⏸ DEFERRED.

Runs the real ``generic_price_spider`` (via ``scrapy.crawler.CrawlerProcess``
in its own OS process — Twisted's reactor can only start once per process,
see ``_scrapyd_spider_live_support.run_generic_price_spider_subprocess``)
against ``tests/fixtures/html/jsonld_product.html`` served by a loopback
``http.server`` — no real-competitor network call is ever made
(FR-021/SC-007). The crawl's ``DNS_RESOLVER`` is overridden with a
test-only resolver subclass that maps the fixture's hostname straight to
``127.0.0.1`` (never touching real DNS), which is exactly the injectable
resolver/allowlist seam ``contracts/fetch-url-safety.md`` describes for a
happy-path fixture test — production wiring
(``scrape_core.safety.resolver.SafeResolver``, no allowlist) is never
touched.

Proves the full US1 acceptance scenario end to end:

1. One workspace/product/variant/competitor/match/scrape-profile is
   seeded; the match's ``competitor_url`` points at the loopback fixture
   server.
2. After the spider run, exactly **one** ``price_observations`` row
   exists for the match: ``success=true``, ``price=129.99``,
   ``currency=USD``, ``extraction_method=JSON_LD``,
   ``extraction_confidence=0.95`` (>= the 0.75 default acceptance
   threshold), all scoped to the seeded workspace.
3. ``match_current_prices`` has exactly one upserted row for
   ``(workspace_id, match_id)`` carrying the same price.
4. Exactly **one** ``request_attempts`` row exists for the match
   (``access_method=DIRECT_HTTP``, ``success=true``, ``status_code=200``).
5. The spider stops at persistence (FR-020) — no alert/variant-state/
   webhook table exists to pollute in this slice, so there is nothing
   further to assert there.

Needs a reachable Postgres (``DATABASE_URL``) with the SPEC-07 migration
applied (``price_observations``/``request_attempts``/
``match_current_prices``/``competitor_product_matches``/``competitors``/
``product_variants``/``products``/``workspaces``/``scrape_profiles``
tables present) AND a reachable Redis (``REDIS_URL`` — the spider's
profile-resolution cache lookup touches Redis even on a cache miss). Not
runnable in the no-Docker-daemon build environment used to author this
feature — SKIPS cleanly whenever either isn't usable, a real connection
attempt fails, or the required tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-07 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
    run_generic_price_spider_subprocess,
    seed_competitor,
    seed_match,
    seed_scrape_profile,
    seed_workspace_with_variant,
    serve_fixture_pages,
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
        "scrape_profiles",
    }
)

pytestmark = pytest.mark.skipif(
    not live_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-07 observations "
        "migration applied -- not available in this environment."
    ),
)

# Resolver source embedded in the generated runner script (see
# `run_generic_price_spider_subprocess`'s docstring): maps the fixture's
# ".invalid" hostname straight to the loopback fixture server's IP, never
# touching real DNS. `fixture-store.invalid` is also the hostname the
# JSON-LD fixture's own `offers.url` uses (see jsonld_product.html) so the
# match URL and the fixture's own content agree on a fully-invented host.
_RESOLVER_SOURCE = """
from twisted.internet import defer

from scrape_core.safety.resolver import SafeResolver


class _TestResolver(SafeResolver):
    def getHostByName(self, name, timeout=()):
        if name == "fixture-store.invalid":
            return defer.succeed("127.0.0.1")
        return super().getHostByName(name, timeout)
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec07-jsonld")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def test_jsonld_fixture_spider_run_persists_one_success_observation(
    seeded: SeededWorkspace,
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    html = (_FIXTURES_DIR / "jsonld_product.html").read_text(encoding="utf-8")
    server, thread, port = serve_fixture_pages({"/product": html})
    try:
        competitor_id = seed_competitor(seeded, "jsonld-fixture-competitor")
        profile_id = seed_scrape_profile(seeded, "jsonld-fixture-profile")
        match_url = f"http://fixture-store.invalid:{port}/product"
        match_id = seed_match(
            seeded, competitor_id, match_url, scrape_profile_id=profile_id
        )
        scrape_job_id = uuid.uuid4()

        result = run_generic_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
            resolver_source=_RESOLVER_SOURCE,
            dns_resolver_dotted_path="__main__._TestResolver",
        )
        assert result.returncode == 0, result.stderr

        with get_session() as session:
            observations = session.execute(
                text(
                    "SELECT price, currency, extraction_method, extraction_confidence, "
                    "success, comparable, scrape_job_id "
                    "FROM price_observations WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert len(observations) == 1, observations
            row = observations[0]
            assert row.success is True
            assert row.comparable is True
            assert row.price == Decimal("129.99")
            assert row.currency == "USD"
            assert row.extraction_method == "JSON_LD"
            assert row.extraction_confidence >= Decimal("0.75")
            assert str(row.scrape_job_id) == str(scrape_job_id)

            attempts = session.execute(
                text(
                    "SELECT status_code, access_method, success "
                    "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert len(attempts) == 1, attempts
            assert attempts[0].status_code == 200
            assert attempts[0].access_method == "DIRECT_HTTP"
            assert attempts[0].success is True

            current_prices = session.execute(
                text(
                    "SELECT price, currency, success "
                    "FROM match_current_prices WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(match_id)},
            ).fetchall()
            assert len(current_prices) == 1, current_prices
            assert current_prices[0].price == Decimal("129.99")
            assert current_prices[0].currency == "USD"
            assert current_prices[0].success is True
    finally:
        server.shutdown()
        server.server_close()
