"""Fixture-driven adapter and central product-identity tests."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from app_shared.enums import AdapterKey, ExtractionMethod, StockStatus

from scrape_core.adapters import (
    AdapterContext,
    AdapterOutcome,
    AdapterResponse,
    IdentityStatus,
    get_adapter,
)
from scrape_core.adapters.config import AdapterConfigurationError
from scrape_core.adapters.identity import validate_final_url
from scrape_core.validation import Accepted, Rejected, parse_optional_old_price, validate_candidate

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "adapters"
_HTML_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class _Profile:
    adapter_config: dict
    jsonld_enabled: bool = True
    confidence_rules: dict | None = None
    validation_rules: dict | None = None


def _catalog_profile(**overrides: object) -> _Profile:
    config = {
        "endpoint_template": "https://catalog.example/search?q={identifier_urlencoded}",
        "identifier_sources": ["competitor_variant_identifier", "sku"],
        "items_paths": ["/hits"],
        "exact_id_paths": ["sku"],
        "fields": {
            "sale_price": ["sale_price"],
            "price": ["price"],
            "currency": ["currency"],
            "stock": ["is_buyable"],
            "title": ["name"],
            "url": ["url"],
        },
        "currency": "SAR",
    }
    config.update(overrides)
    return _Profile(config)


def test_registry_exposes_html_rendered_shopify_catalog_and_repair() -> None:
    for key in (
        AdapterKey.DEFAULT_HTTP,
        AdapterKey.PLAYWRIGHT_RENDERED,
        AdapterKey.SHOPIFY_PRODUCT_JSON,
        AdapterKey.PUBLIC_CATALOG_JSON,
        AdapterKey.EXACT_ID_URL_REPAIR,
    ):
        assert get_adapter(key) is not None


def test_default_and_rendered_html_preserve_the_existing_extraction_chain() -> None:
    body = (_HTML_FIXTURES / "jsonld_product.html").read_text(encoding="utf-8")
    context = AdapterContext("https://shop.example/products/widget", profile=_Profile({}))
    response = AdapterResponse(body, context.target_url)

    for key in (AdapterKey.DEFAULT_HTTP, AdapterKey.PLAYWRIGHT_RENDERED):
        result = get_adapter(key).adapt(response, context)
        assert result.outcome == AdapterOutcome.FOUND
        assert result.candidate is not None
        assert result.candidate.method == ExtractionMethod.JSON_LD


def test_catalog_request_prefers_variant_identifier_and_url_encodes_it() -> None:
    context = AdapterContext(
        "https://shop.example/product",
        variant_identifier="Z PRODUCT/42",
        sku="fallback",
        profile=_catalog_profile(),
    )
    request = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).build_request(context)
    assert request.url == "https://catalog.example/search?q=Z%20PRODUCT%2F42"


def test_catalog_exact_match_never_substitutes_fuzzy_result_and_keeps_list_price() -> None:
    context = AdapterContext(
        "https://www.noon.com/saudi-en/product/Z-EXACT-42/p/",
        variant_identifier="Z-EXACT-42",
        profile=_catalog_profile(),
    )
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse(_fixture("catalog_noon.json"), "https://catalog.example/search"), context
    )

    assert result.outcome == AdapterOutcome.FOUND
    assert result.identity.status == IdentityStatus.VALID
    assert result.identity.observed_identifier == "Z-EXACT-42"
    assert result.candidate is not None
    assert result.candidate.raw_price_text == "79.5"
    assert result.candidate.currency == "SAR"
    assert result.candidate.stock == StockStatus.IN_STOCK
    assert result.candidate.method == ExtractionMethod.PLATFORM_JSON
    assert result.old_price_text == "99"
    assert result.canonical_url == "https://www.noon.com/saudi-en/exact/Z-EXACT-42/p/"
    accepted = validate_candidate(result.candidate, {"required_currency": "SAR"})
    assert isinstance(accepted, Accepted)
    assert parse_optional_old_price(result.old_price_text, current_price=accepted.price) == 99


def test_catalog_fuzzy_only_response_is_terminal_not_listed() -> None:
    context = AdapterContext(
        "https://www.noon.com/product/N-MISSING/p/",
        variant_identifier="N-MISSING",
        profile=_catalog_profile(),
    )
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse(_fixture("catalog_noon.json"), "https://catalog.example/search"), context
    )
    assert result.outcome == AdapterOutcome.NOT_LISTED
    assert result.candidate is None


@pytest.mark.parametrize("identifier", ["N-EXACT-1", "Z-EXACT-1"])
def test_catalog_identifier_matching_is_prefix_agnostic(identifier: str) -> None:
    context = AdapterContext(
        "https://shop.example/product", variant_identifier=identifier, profile=_catalog_profile()
    )
    body = '{"hits":[{"sku":"' + identifier + '","price":10,"is_buyable":true}]}'
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse(body, "https://catalog.example/search"), context
    )
    assert result.outcome == AdapterOutcome.FOUND


def test_catalog_zero_sale_price_falls_back_to_regular_price_and_stock_false() -> None:
    profile = _catalog_profile(
        items_paths=["/response/products"],
        exact_id_paths=["uniqueId"],
        fields={
            "sale_price": ["salePrice"],
            "price": ["price"],
            "currency": ["currencyCode"],
            "stock": ["available"],
            "title": ["title"],
            "url": ["productUrl"],
        },
    )
    context = AdapterContext("https://extra.example/100200", variant_identifier="100200", profile=profile)
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse(_fixture("catalog_extra.json"), "https://catalog.extra.example/search"), context
    )
    assert result.candidate is not None
    assert result.candidate.raw_price_text == "2499"
    assert result.candidate.stock == StockStatus.OUT_OF_STOCK
    assert result.old_price_text is None


def test_catalog_uses_profile_decimal_transform_without_float_rounding() -> None:
    profile = _catalog_profile()
    profile.price_transform_rules = {"price": {"divide_by": 100}}
    context = AdapterContext("https://shop.example/item", variant_identifier="x", profile=profile)
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse('{"hits":[{"sku":"x","price":10005}]}', "https://catalog.example"), context
    )
    assert result.candidate is not None
    assert result.candidate.raw_price_text == "100.05"


@pytest.mark.parametrize("body", ["{bad json", "null", "[]"])
def test_catalog_malformed_or_wrong_shape_never_raises(body: str) -> None:
    context = AdapterContext("https://shop.example/x", variant_identifier="x", profile=_catalog_profile())
    result = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse(body, "https://catalog.example/search"), context
    )
    assert result.outcome in (AdapterOutcome.INVALID_RESPONSE, AdapterOutcome.NOT_LISTED)
    assert result.candidate is None


def test_catalog_missing_price_is_not_found_and_zero_price_still_crosses_money_boundary() -> None:
    context = AdapterContext("https://shop.example/x", variant_identifier="x", profile=_catalog_profile())
    missing = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse('{"hits":[{"sku":"x","is_buyable":true}]}', "https://catalog.example"), context
    )
    assert missing.outcome == AdapterOutcome.NOT_FOUND

    zero = get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).adapt(
        AdapterResponse('{"hits":[{"sku":"x","price":0,"is_buyable":true}]}', "https://catalog.example"), context
    )
    assert zero.candidate is not None
    assert isinstance(validate_candidate(zero.candidate, None), Rejected)


def test_shopify_exact_variant_minor_units_list_price_stock_and_currency() -> None:
    profile = _Profile({"currency": "SAR"})
    context = AdapterContext(
        "https://stech.example/products/universal-toner?variant=7001",
        variant_identifier="BLACK-XL",
        profile=profile,
    )
    adapter = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON)
    assert adapter.build_request(context).url == "https://stech.example/products/universal-toner.js"
    result = adapter.adapt(
        AdapterResponse(_fixture("shopify_product.json"), adapter.build_request(context).url), context
    )
    assert result.outcome == AdapterOutcome.FOUND
    assert result.candidate is not None
    assert result.candidate.raw_price_text == "129.99"
    assert result.old_price_text == "149.99"
    assert result.candidate.currency == "SAR"
    assert result.candidate.stock == StockStatus.IN_STOCK
    assert isinstance(validate_candidate(result.candidate, {"required_currency": "SAR"}), Accepted)


def test_optional_old_price_uses_money_boundary_without_rejecting_current_offer() -> None:
    assert parse_optional_old_price("SAR 149.99", current_price=129) == Decimal("149.99")
    assert parse_optional_old_price("not money", current_price=129) is None
    assert parse_optional_old_price("129", current_price=129) is None


def test_shopify_unavailable_variant_keeps_public_price_and_marks_oos() -> None:
    context = AdapterContext(
        "https://stech.example/products/universal-toner",
        variant_identifier="CYAN-XL",
        expected_currency="SAR",
        profile=_Profile({}),
    )
    result = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON).adapt(
        AdapterResponse(_fixture("shopify_product.json"), "https://stech.example/products/universal-toner.js"),
        context,
    )
    assert result.candidate is not None
    assert result.candidate.raw_price_text == "109.5"
    assert result.candidate.stock == StockStatus.OUT_OF_STOCK


def test_shopify_rejects_cross_product_handle_and_fuzzy_variant() -> None:
    wrong_product = _fixture("shopify_product.json").replace("universal-toner", "other-toner")
    product_context = AdapterContext(
        "https://stech.example/products/universal-toner", profile=_Profile({})
    )
    mismatch = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON).adapt(
        AdapterResponse(wrong_product, "https://stech.example/products/universal-toner.js"), product_context
    )
    assert mismatch.outcome == AdapterOutcome.IDENTITY_MISMATCH

    # EPA B4 (2026-08-25): this assertion used to read `== NOT_LISTED`,
    # and that expectation WAS the bug. "BLACK" is an untyped legacy
    # value that matches no variant field on a two-variant product — the
    # store is plainly serving the product, so the honest answer is "we
    # cannot tell which variant", never "the competitor delisted it".
    # Reading a failed string match as a delisting is what produced 26
    # false NOT_LISTED verdicts on S-Tech in the 2026-08-24 canary; that
    # verdict now requires repeatedly validated absence of the product
    # itself (see scrape_core.adapters.variant_resolution).
    variant_context = AdapterContext(
        product_context.target_url, variant_identifier="BLACK", profile=_Profile({})
    )
    missing = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON).adapt(
        AdapterResponse(_fixture("shopify_product.json"), "https://stech.example/products/universal-toner.js"),
        variant_context,
    )
    assert missing.outcome == AdapterOutcome.IDENTITY_UNRESOLVED
    assert missing.outcome != AdapterOutcome.NOT_LISTED
    assert missing.identity.status == IdentityStatus.UNRESOLVED


def test_shopify_malformed_and_missing_price_are_safe() -> None:
    context = AdapterContext("https://stech.example/products/universal-toner", profile=_Profile({}))
    malformed = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON).adapt(
        AdapterResponse("{bad", "https://stech.example/products/universal-toner.js"), context
    )
    assert malformed.outcome == AdapterOutcome.INVALID_RESPONSE

    no_price = _fixture("shopify_product.json").replace('"price": 12999', '"price": null').replace(
        '"available": true', '"available": false', 1
    )
    result = get_adapter(AdapterKey.SHOPIFY_PRODUCT_JSON).adapt(
        AdapterResponse(no_price, "https://stech.example/products/universal-toner.js"), context
    )
    # EPA B4 (2026-08-25): previously this fell back to "the first
    # product variant, deterministically". A default variant is only ever
    # legitimate when the product HAS one variant; on a two-variant
    # product with no identifier at all, picking one is a guess, and a
    # wrong price is worse than a missing one. The adapter now says so
    # explicitly instead of silently choosing.
    assert result.outcome == AdapterOutcome.IDENTITY_UNRESOLVED
    assert result.candidate is None


@pytest.mark.parametrize(
    ("requested", "final", "expected", "rules", "status"),
    [
        (
            "https://shop.example/en/product/ABC-1",
            "https://shop.example/en/product/ABC-1",
            "ABC-1",
            {"url_id_pattern": r"/product/(?P<id>[A-Z0-9-]+)$"},
            IdentityStatus.VALID,
        ),
        (
            "https://shop.example/en/old-slug/ABC-1/p/",
            "https://shop.example/ar/new-slug/ABC-1/p/",
            "ABC-1",
            {"url_id_pattern": r"/(?P<id>[A-Z0-9-]+)/p/?$"},
            IdentityStatus.VALID,
        ),
        (
            "https://shop.example/product/ABC-1",
            "https://shop.example/",
            "ABC-1",
            {"url_id_pattern": r"/product/(?P<id>[A-Z0-9-]+)$"},
            IdentityStatus.NOT_LISTED,
        ),
        (
            "https://shop.example/product/ABC-1",
            "https://shop.example/product/XYZ-2",
            "ABC-1",
            {"url_id_pattern": r"/product/(?P<id>[A-Z0-9-]+)$"},
            IdentityStatus.MISMATCH,
        ),
        (
            "https://shop.example/product/ABC-1",
            "https://other.example/product/ABC-1",
            "ABC-1",
            {"url_id_pattern": r"/product/(?P<id>[A-Z0-9-]+)$"},
            IdentityStatus.MISMATCH,
        ),
    ],
)
def test_central_identity_validation(
    requested: str, final: str, expected: str, rules: dict, status: IdentityStatus
) -> None:
    assert validate_final_url(
        requested, final, expected_identifier=expected, rules=rules
    ).status == status


def test_html_adapter_never_extracts_homepage_featured_price_after_root_redirect() -> None:
    homepage = (_HTML_FIXTURES / "jsonld_product.html").read_text(encoding="utf-8")
    profile = _Profile({"identity_rules": {"url_id_pattern": r"/products/(?P<id>[^/]+)$"}})
    context = AdapterContext(
        "https://shop.example/products/deleted-widget", identifier="deleted-widget", profile=profile
    )
    result = get_adapter(AdapterKey.DEFAULT_HTTP).adapt(
        AdapterResponse(homepage, "https://shop.example/", requested_url=context.target_url), context
    )
    assert result.outcome == AdapterOutcome.NOT_LISTED
    assert result.candidate is None


def test_priced_scoped_offer_is_not_overridden_by_unrelated_oos_page_text() -> None:
    html = """
      <script type="application/ld+json">
      {"@type":"Product","name":"Current item","offers":{"@type":"Offer","price":"88.00","priceCurrency":"SAR","availability":"https://schema.org/InStock"}}
      </script>
      <aside>Customers also viewed: old accessory — out of stock</aside>
    """
    context = AdapterContext("https://shop.example/products/current", profile=_Profile({}))
    result = get_adapter(AdapterKey.PLAYWRIGHT_RENDERED).adapt(
        AdapterResponse(html, context.target_url), context
    )
    assert result.candidate is not None
    assert result.candidate.stock == StockStatus.IN_STOCK


def test_exact_id_repair_returns_changed_slug_only_for_same_id() -> None:
    profile = _Profile(
        {
            "repair_endpoint_template": "https://shop.example/search?q={identifier_urlencoded}",
            "identifier_sources": ["identifier"],
            "items_paths": ["/products"],
            "exact_id_paths": ["uniqueId"],
            "url_paths": ["url"],
            "identity_rules": {"url_id_pattern": r"/(?P<id>[0-9]+)/p/?$"},
        }
    )
    context = AdapterContext("https://shop.example/saudi-en/old-slug/555/p/", identifier="555", profile=profile)
    result = get_adapter(AdapterKey.EXACT_ID_URL_REPAIR).adapt(
        AdapterResponse(_fixture("repair_results.json"), "https://shop.example/search"), context
    )
    assert result.outcome == AdapterOutcome.REPAIRED
    assert result.canonical_url == "https://shop.example/saudi-en/changed-slug/555/p/"

    missing_context = AdapterContext(context.target_url, identifier="557", profile=profile)
    missing = get_adapter(AdapterKey.EXACT_ID_URL_REPAIR).adapt(
        AdapterResponse(_fixture("repair_results.json"), "https://shop.example/search"), missing_context
    )
    assert missing.outcome == AdapterOutcome.NOT_LISTED


def test_html_exact_id_repair_rejects_fuzzy_links_and_accepts_changed_slug() -> None:
    profile = _Profile(
        {
            "endpoint_template": "https://shop.example/search?q={identifier}",
            "identifier_sources": ["identifier"],
            "response_format": "html",
            "link_selector": "a.result::attr(href)",
            "identity_rules": {"url_id_pattern": r"/(?P<id>[0-9]+)/p/?$"},
        }
    )
    context = AdapterContext("https://shop.example/old/555/p/", identifier="555", profile=profile)
    body = '<a class="result" href="/similar/556/p/">fuzzy</a><a class="result" href="/new/555/p/">exact</a>'
    result = get_adapter(AdapterKey.EXACT_ID_URL_REPAIR).adapt(
        AdapterResponse(body, "https://shop.example/search?q=555"), context
    )
    assert result.outcome == AdapterOutcome.REPAIRED
    assert result.canonical_url == "https://shop.example/new/555/p/"


def test_unknown_endpoint_placeholder_fails_before_network() -> None:
    context = AdapterContext(
        "https://shop.example/product",
        variant_identifier="x",
        profile=_catalog_profile(endpoint_template="https://catalog.example/{customer_name}"),
    )
    with pytest.raises(AdapterConfigurationError):
        get_adapter(AdapterKey.PUBLIC_CATALOG_JSON).build_request(context)
