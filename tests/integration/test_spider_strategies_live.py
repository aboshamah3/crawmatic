"""Live multi-strategy extraction + validation spider run (SPEC-07 US3 T046,
FR-006..FR-011, SC-003) — ⏸ DEFERRED.

Runs the real ``generic_price_spider`` (via
``run_generic_price_spider_subprocess``) against three fixtures in one
crawl, each served by the same loopback fixture server at a distinct
path, over a fully-invented ``.invalid`` hostname resolver-mapped straight
to the loopback address (see `test_spider_jsonld_fixture_live.py` for the
identical resolver-allowlist pattern) — zero real-competitor network
calls (FR-021/SC-007):

1. ``css_only.html`` + a CSS-selector-only profile
   (``price_selector="span.price"`` etc., matching the fixture's own
   docstring) — no JSON-LD present, so extraction falls through to CSS:
   ``extraction_method=CSS``, ``extraction_confidence=0.85``,
   ``price=74.50``, ``currency=USD``, ``success=true``.
2. ``regex_only.html`` + a regex-only profile (``price_regex``/
   ``currency_regex`` matching the fixture's own docstring) — no JSON-LD,
   no CSS-selectable price element, so extraction falls through to regex:
   ``extraction_method=REGEX``, ``extraction_confidence=0.75``,
   ``price=56.25``, ``currency=USD``, ``success=true``.
3. ``discount_save_x.html`` + a profile whose ``price_regex`` is broad
   enough to surface the promo div's "$10" ("Save $10 today") as a
   regex candidate (mirroring
   `tests/unit/test_price_validation.py`'s
   ``test_discount_save_x_fixture_regex_candidate_is_rejected_end_to_end``)
   and whose ``validation_rules.reject_if_text_contains`` includes
   "save"/"old"/"installment"/"discount"/"shipping" — the candidate is
   extracted but then rejected by ``validate_candidate``'s text-reject
   check: ``success=false``, ``error_code=PRICE_NOT_FOUND``, no
   observation is ever ``success=true`` for this match, and
   ``match_current_prices`` is never upserted for it.

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

_HOST = "fixture-store.invalid"

_RESOLVER_SOURCE = f"""
from twisted.internet import defer

from scrape_core.safety.resolver import SafeResolver


class _TestResolver(SafeResolver):
    def getHostByName(self, name, timeout=()):
        if name == {_HOST!r}:
            return defer.succeed("127.0.0.1")
        return super().getHostByName(name, timeout)
"""


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec07-strategies")
    try:
        yield workspace
    finally:
        cleanup_seeded_workspace(workspace)


def test_css_and_regex_fixtures_extract_and_discount_only_fixture_is_rejected(
    seeded: SeededWorkspace,
) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    css_html = (_FIXTURES_DIR / "css_only.html").read_text(encoding="utf-8")
    regex_html = (_FIXTURES_DIR / "regex_only.html").read_text(encoding="utf-8")
    discount_html = (_FIXTURES_DIR / "discount_save_x.html").read_text(encoding="utf-8")

    server, thread, port = serve_fixture_pages(
        {
            "/css-only": css_html,
            "/regex-only": regex_html,
            "/discount-only": discount_html,
        }
    )
    try:
        competitor_id = seed_competitor(seeded, "strategies-fixture-competitor")

        css_profile_id = seed_scrape_profile(
            seeded,
            "css-fixture-profile",
            price_selector="span.price",
            old_price_selector="span.old-price",
            currency_selector="span.currency",
            stock_selector="span.stock",
            title_selector="h1.product-title",
        )
        regex_profile_id = seed_scrape_profile(
            seeded,
            "regex-fixture-profile",
            price_regex=r'"price"\s*:\s*"?([0-9.,]+)',
            currency_regex=r'"currency"\s*:\s*"([A-Z]{3})"',
        )
        discount_profile_id = seed_scrape_profile(
            seeded,
            "discount-fixture-profile",
            price_regex=r"\$([0-9]+(?:\.[0-9]{2})?)",
            validation_rules={
                "reject_if_text_contains": [
                    "old",
                    "installment",
                    "discount",
                    "save",
                    "shipping",
                ]
            },
        )

        css_match_id = seed_match(
            seeded,
            competitor_id,
            f"http://{_HOST}:{port}/css-only",
            scrape_profile_id=css_profile_id,
        )
        regex_match_id = seed_match(
            seeded,
            competitor_id,
            f"http://{_HOST}:{port}/regex-only",
            scrape_profile_id=regex_profile_id,
        )
        discount_match_id = seed_match(
            seeded,
            competitor_id,
            f"http://{_HOST}:{port}/discount-only",
            scrape_profile_id=discount_profile_id,
        )

        result = run_generic_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=uuid.uuid4(),
            match_ids=[css_match_id, regex_match_id, discount_match_id],
            resolver_source=_RESOLVER_SOURCE,
            dns_resolver_dotted_path="__main__._TestResolver",
        )
        assert result.returncode == 0, result.stderr

        with get_session() as session:
            css_row = session.execute(
                text(
                    "SELECT price, currency, extraction_method, extraction_confidence, success "
                    "FROM price_observations WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(css_match_id)},
            ).fetchone()
            assert css_row is not None
            assert css_row.success is True
            assert css_row.price == Decimal("74.50")
            assert css_row.currency == "USD"
            assert css_row.extraction_method == "CSS"
            assert css_row.extraction_confidence == Decimal("0.8500")

            regex_row = session.execute(
                text(
                    "SELECT price, currency, extraction_method, extraction_confidence, success "
                    "FROM price_observations WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(regex_match_id)},
            ).fetchone()
            assert regex_row is not None
            assert regex_row.success is True
            assert regex_row.price == Decimal("56.25")
            assert regex_row.currency == "USD"
            assert regex_row.extraction_method == "REGEX"
            assert regex_row.extraction_confidence == Decimal("0.7500")

            discount_row = session.execute(
                text(
                    "SELECT success, error_code FROM price_observations "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(discount_match_id)},
            ).fetchone()
            assert discount_row is not None
            assert discount_row.success is False
            assert discount_row.error_code == "PRICE_NOT_FOUND"

            discount_current_price = session.execute(
                text(
                    "SELECT match_id FROM match_current_prices "
                    "WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": str(seeded.workspace_id), "match": str(discount_match_id)},
            ).fetchone()
            assert discount_current_price is None
    finally:
        server.shutdown()
        server.server_close()
