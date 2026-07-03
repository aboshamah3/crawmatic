"""Unit tests for `app_shared.catalog.default_variant` (T015, FR-005/FR-006, SC-001).

Pure, DB-independent — exercises `derive_default_variant`/
`ensure_at_least_one` against plain dicts only.
"""

from __future__ import annotations

from decimal import Decimal

from app_shared.catalog.default_variant import (
    derive_default_variant,
    ensure_at_least_one,
)
from app_shared.enums import VariantStatus


# --- derive_default_variant: title -----------------------------------------


def test_derive_default_variant_title_inherits_product_title() -> None:
    product = {"title": "Acme Widget", "price": Decimal("9.99"), "currency": "USD"}
    variant = derive_default_variant(product)
    assert variant["title"] == "Acme Widget"


def test_derive_default_variant_title_falls_back_to_default_when_missing() -> None:
    product = {"price": Decimal("9.99"), "currency": "USD"}
    variant = derive_default_variant(product)
    assert variant["title"] == "Default"


def test_derive_default_variant_title_falls_back_to_default_when_empty_string() -> None:
    product = {"title": "", "price": Decimal("9.99"), "currency": "USD"}
    variant = derive_default_variant(product)
    assert variant["title"] == "Default"


# --- derive_default_variant: sku/url/price/currency inheritance ------------


def test_derive_default_variant_inherits_sku_and_url_when_present() -> None:
    product = {
        "title": "Acme Widget",
        "sku": "ACME-1",
        "url": "https://example.com/acme-1",
        "price": Decimal("9.99"),
        "currency": "USD",
    }
    variant = derive_default_variant(product)
    assert variant["sku"] == "ACME-1"
    assert variant["url"] == "https://example.com/acme-1"


def test_derive_default_variant_sku_and_url_default_to_none_when_absent() -> None:
    product = {"title": "Acme Widget", "price": Decimal("9.99"), "currency": "USD"}
    variant = derive_default_variant(product)
    assert variant["sku"] is None
    assert variant["url"] is None


def test_derive_default_variant_inherits_price_and_currency_from_payload() -> None:
    product = {"title": "Acme Widget", "price": Decimal("19.5000"), "currency": "EUR"}
    variant = derive_default_variant(product)
    assert variant["current_price"] == Decimal("19.5000")
    assert variant["currency"] == "EUR"


def test_derive_default_variant_option_values_none_and_status_active() -> None:
    product = {"title": "Acme Widget", "price": Decimal("9.99"), "currency": "USD"}
    variant = derive_default_variant(product)
    assert variant["option_values"] is None
    assert variant["status"] == VariantStatus.ACTIVE


# --- ensure_at_least_one -----------------------------------------------------


def test_ensure_at_least_one_passes_through_non_empty_variants_unchanged() -> None:
    product = {"title": "Acme Widget"}
    explicit_variants = [{"title": "Red"}, {"title": "Blue"}]
    result = ensure_at_least_one(product, explicit_variants)
    assert result is explicit_variants
    assert result == [{"title": "Red"}, {"title": "Blue"}]


def test_ensure_at_least_one_derives_single_default_when_empty() -> None:
    product = {"title": "Acme Widget", "price": Decimal("9.99"), "currency": "USD"}
    result = ensure_at_least_one(product, [])
    assert len(result) == 1
    assert result[0]["title"] == "Acme Widget"
    assert result[0]["current_price"] == Decimal("9.99")
    assert result[0]["currency"] == "USD"


def test_ensure_at_least_one_never_adds_extra_default_when_variants_given() -> None:
    product = {"title": "Acme Widget"}
    explicit_variants = [{"title": "Solo Variant"}]
    result = ensure_at_least_one(product, explicit_variants)
    assert len(result) == 1
