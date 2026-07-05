"""Unit test: SPEC-12 reuses the shipped `app_shared.url_pattern` derivation verbatim
(T014, US1, AS4, FR-001..FR-004, D10).

No new derivation code is authored for SPEC-12 -- this test only asserts
the already-shipped `derive_url_pattern`/`URL_PATTERN_ALGORITHM_VERSION`
(SPEC-05, `tests/unit/test_url_pattern.py`) group SPEC-12's own worked
examples correctly, including the locale-prefix and `:id` edge cases
named in the spec's Edge Cases section, so US1's grouping step is
regression-proofed independently of the SPEC-05 corpus.
"""

from __future__ import annotations

from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION, derive_url_pattern


def test_two_differing_product_urls_group_under_one_pattern() -> None:
    a = derive_url_pattern("https://www.example.com/products/red-shoe-123")
    b = derive_url_pattern("http://example.com/products/blue-shoe-999?ref=x#frag")
    assert a == "example.com/products/*"
    assert b == "example.com/products/*"
    assert a == b


def test_pattern_is_stamped_at_the_current_algorithm_version() -> None:
    # SPEC-12 stamps `url_pattern_version` from this constant at write
    # time (FR-004) -- the reused derivation is not re-versioned here.
    assert isinstance(URL_PATTERN_ALGORITHM_VERSION, int)
    assert URL_PATTERN_ALGORITHM_VERSION == 1


def test_locale_prefix_is_preserved_across_scheme_and_www_variants() -> None:
    a = derive_url_pattern("https://www.example.com/ar/products/red-shoe-123")
    b = derive_url_pattern("http://example.com/ar/products/blue-shoe-999?ref=x#frag")
    assert a == "example.com/ar/products/*"
    assert a == b


def test_id_like_segment_not_after_a_product_key_becomes_colon_id() -> None:
    # A numeric/opaque segment outside the "products/product/p/item"
    # wildcard rule generalizes to `:id`, not `*` -- the two
    # generalizations are distinct (contracts/url-pattern.md).
    assert derive_url_pattern("https://example.com/orders/123456") == (
        "example.com/orders/:id"
    )
    assert derive_url_pattern(
        "https://example.com/orders/550e8400-e29b-41d4-a716-446655440000"
    ) == "example.com/orders/:id"
