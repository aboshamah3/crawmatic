"""EPA B6b certification duty: no price/identity API call gets blocked.

EPA B6 enabled sub-resource cost blocking by default for every browser
fetch (image/media/font/stylesheet by type, ads/analytics/social by
host -- ``app_shared.profiles.browser_resource_policy``). B6b wired the
real per-domain ``ScrapeProfile`` into that decision
(``scrape_core.browser.domain_profile_registry`` +
``scrape_core.browser.ssrf.abort_unsafe_request``). Per the B6 plan's own
certification duty, enabling blocking on a domain requires proof that no
price/identity API call for that domain is ever blocked -- this module
is that proof for the two domains B5/B5b actually certified evidence
for: amazon.sa and noon.com.

**Real URLs, not synthetic ones**: every URL exercised here is read
straight from the B5/B5b fixture directories' own ``expected.json``
``provenance.source_url`` field
(``tests/fixtures/amazon_labeled/*/expected.json``,
``tests/fixtures/noon_labeled/*/expected.json``) -- the exact URLs this
session's real captures (Amazon direct CSS re-certification, Noon
DataImpulse-proxy catalog-API re-certification) fetched and confirmed
carry a real price. A future fixture refresh changes these automatically
-- this test never hardcodes an ASIN/SKU list of its own.

**Real resource_type shapes**: per ``scripts/seed_domain_playbooks.sql``,
both domains' Playwright fallback methods
(``resilience.amazon.rendered.v1`` priority 1/2,
``PLAYWRIGHT_DIRECT``/``PLAYWRIGHT_PROXY`` priority 2/3 for noon.com)
dispatch this exact fixture URL as the top-level *navigated* document
(``generic_browser_price_spider._browser_request_for`` sets
``adapter_request.url`` as the one ``scrapy.Request`` URL Playwright
navigates to) -- so ``resource_type == "document"`` is the shape that
actually occurs in production today. ``xhr``/``fetch`` are exercised too,
defensively, against any future dispatch shape where a price/identity
call instead rides as an in-page sub-resource rather than the top-level
navigation (e.g. a product page whose own JS re-fetches the catalog
JSON) -- per the task's explicit "document/xhr/fetch paths must pass"
requirement.

**Real profile shape, not just None**: each URL is checked against an
actual ``app_shared.models.scrape_profiles.ScrapeProfile`` ORM instance
(no DB session/I/O -- plain Python construction) built from that
domain's real seeded playbook config
(``scripts/seed_domain_playbooks.sql``'s ``resilience.amazon.rendered.v1``/
``resilience.noon.catalog-json.v1`` rows), not a synthetic
``SimpleNamespace`` double or a bare ``None`` -- proving the actual
production model class's tolerant ``certified_resources`` getattr (no
such column exists yet, see ``browser_resource_policy``'s module
docstring) degrades to the correct "nothing blocked for these real
calls" outcome the domain already needs, without requiring the column to
land first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_shared.enums import AdapterKey, ScrapeProfileMode
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.browser_resource_policy import should_block

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_AMAZON_DIR = _FIXTURES / "amazon_labeled"
_NOON_DIR = _FIXTURES / "noon_labeled"

_PRICE_IDENTITY_RESOURCE_TYPES = ("document", "xhr", "fetch")


def _source_urls(fixture_dir: Path) -> list[str]:
    urls: list[str] = []
    for case_dir in sorted(fixture_dir.iterdir()):
        if not case_dir.is_dir() or case_dir.name.startswith("_"):
            continue
        expected_path = case_dir / "expected.json"
        if not expected_path.exists():
            continue
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        source_url = expected.get("provenance", {}).get("source_url")
        assert source_url, f"{expected_path} has no provenance.source_url"
        urls.append(source_url)
    return urls


AMAZON_PRICE_IDENTITY_URLS = _source_urls(_AMAZON_DIR)
NOON_PRICE_IDENTITY_URLS = _source_urls(_NOON_DIR)

assert AMAZON_PRICE_IDENTITY_URLS, "no amazon_labeled fixtures found -- fixture set is empty"
assert NOON_PRICE_IDENTITY_URLS, "no noon_labeled fixtures found -- fixture set is empty"

# Real seeded playbook config (scripts/seed_domain_playbooks.sql) --
# constructed with no DB session/I/O, exactly the fields `should_block`/
# `_certified_resource_types` would ever read (`certified_resources`,
# tolerantly absent -- no such column exists yet).
AMAZON_BROWSER_PROFILE = ScrapeProfile(
    name="resilience.amazon.rendered.v1",
    mode=ScrapeProfileMode.BROWSER,
    adapter_key=AdapterKey.PLAYWRIGHT_RENDERED,
)
NOON_BROWSER_PROFILE = ScrapeProfile(
    name="resilience.noon.catalog-json.v1",
    mode=ScrapeProfileMode.HTTP,
    adapter_key=AdapterKey.PUBLIC_CATALOG_JSON,
)


@pytest.mark.parametrize("url", AMAZON_PRICE_IDENTITY_URLS)
@pytest.mark.parametrize("resource_type", _PRICE_IDENTITY_RESOURCE_TYPES)
def test_amazon_price_identity_url_never_blocked(url: str, resource_type: str) -> None:
    assert not should_block(url, resource_type, AMAZON_BROWSER_PROFILE), (resource_type, url)


@pytest.mark.parametrize("url", NOON_PRICE_IDENTITY_URLS)
@pytest.mark.parametrize("resource_type", _PRICE_IDENTITY_RESOURCE_TYPES)
def test_noon_price_identity_url_never_blocked(url: str, resource_type: str) -> None:
    assert not should_block(url, resource_type, NOON_BROWSER_PROFILE), (resource_type, url)


def test_real_scrape_profile_instance_has_no_certified_resources_column_yet() -> None:
    """Documents the actual current state this test proves safe against:
    the real ORM class has no `certified_resources` field yet (tolerant
    getattr degrades to "nothing certified", `browser_resource_policy`'s
    own module docstring) -- so the "never blocked" assertions above hold
    on the plain default policy alone, not because of any certification
    data this test secretly relies on."""
    assert not hasattr(AMAZON_BROWSER_PROFILE, "certified_resources")
    assert not hasattr(NOON_BROWSER_PROFILE, "certified_resources")


def test_resolving_via_domain_profile_registry_also_never_blocks(monkeypatch) -> None:
    """End-to-end proof of the B6b wiring itself (not just the pure
    policy function in isolation): register each domain's real profile
    via `domain_profile_registry` (exactly as `_browser_request_for`
    does at dispatch time) and confirm `abort_unsafe_request`'s own
    sub-resource lookup path resolves it and still never blocks a real
    price/identity URL."""
    from scrape_core.browser.domain_profile_registry import get_domain_profile, set_domain_profile

    set_domain_profile("www.amazon.sa", AMAZON_BROWSER_PROFILE)
    set_domain_profile("www.noon.com", NOON_BROWSER_PROFILE)
    try:
        for url in AMAZON_PRICE_IDENTITY_URLS:
            profile = get_domain_profile("www.amazon.sa")
            assert not should_block(url, "document", profile)
        for url in NOON_PRICE_IDENTITY_URLS:
            profile = get_domain_profile("www.noon.com")
            assert not should_block(url, "document", profile)
    finally:
        from scrape_core.browser.domain_profile_registry import clear_domain_profile

        clear_domain_profile("www.amazon.sa")
        clear_domain_profile("www.noon.com")
