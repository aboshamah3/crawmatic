"""Unit tests for `app_shared.url_pattern` (T017, US2, FR-010/011, SC-005).

Pure, DB-independent — the normalization + pattern-derivation corpus per
`contracts/url-pattern.md`, including the worked examples.
"""

from __future__ import annotations

from app_shared.url_pattern import (
    URL_PATTERN_ALGORITHM_VERSION,
    derive_match_url_fields,
    derive_url_pattern,
    normalize_url,
)


# --- normalize_url: identity, query kept ------------------------------------


def test_normalize_lowercases_scheme_and_host() -> None:
    assert normalize_url("HTTPS://Competitor.COM/x") == "https://competitor.com/x"


def test_normalize_strips_leading_www() -> None:
    assert normalize_url("https://www.competitor.com/x") == "https://competitor.com/x"


def test_normalize_strips_default_port() -> None:
    assert normalize_url("http://competitor.com:80/p/9f8a7b6c/") == (
        "http://competitor.com/p/9f8a7b6c"
    )
    assert normalize_url("https://competitor.com:443/x") == "https://competitor.com/x"


def test_normalize_keeps_non_default_port() -> None:
    assert normalize_url("http://competitor.com:8080/x") == "http://competitor.com:8080/x"


def test_normalize_removes_fragment() -> None:
    assert normalize_url("https://competitor.com/x#section") == "https://competitor.com/x"


def test_normalize_removes_trailing_slash() -> None:
    assert normalize_url("https://competitor.com/x/") == "https://competitor.com/x"


def test_normalize_bare_host_has_no_trailing_slash() -> None:
    assert normalize_url("https://competitor.com/") == "https://competitor.com"


def test_normalize_keeps_query_string() -> None:
    assert normalize_url("https://competitor.com/x?variant=123") == (
        "https://competitor.com/x?variant=123"
    )


def test_two_urls_differing_only_in_case_www_slash_fragment_normalize_equal() -> None:
    a = normalize_url("HTTPS://WWW.Competitor.com/x/#frag")
    b = normalize_url("https://competitor.com/x")
    assert a == b


def test_two_urls_differing_only_in_query_do_not_normalize_equal() -> None:
    a = normalize_url("https://competitor.com/x?variant=1")
    b = normalize_url("https://competitor.com/x?variant=2")
    assert a != b


# --- derive_url_pattern: grouping, scheme+query dropped --------------------


def test_pattern_drops_scheme_and_query() -> None:
    assert derive_url_pattern("https://competitor.com/x?utm=abc") == "competitor.com/x"


def test_pattern_all_digit_segment_is_id() -> None:
    assert derive_url_pattern("https://competitor.com/catalog/123456?variant=7") == (
        "competitor.com/catalog/:id"
    )


def test_pattern_uuid_like_segment_after_non_product_key_is_id() -> None:
    assert derive_url_pattern(
        "https://competitor.com/orders/550e8400-e29b-41d4-a716-446655440000"
    ) == "competitor.com/orders/:id"


def test_pattern_product_slug_after_known_key_becomes_wildcard() -> None:
    for key in ("products", "product", "p", "item"):
        assert derive_url_pattern(f"https://competitor.com/{key}/iphone-15") == (
            f"competitor.com/{key}/*"
        )


def test_pattern_preserves_locale_prefix() -> None:
    assert derive_url_pattern("https://competitor.com/ar/products/iphone-15") == (
        "competitor.com/ar/products/*"
    )
    assert derive_url_pattern("https://competitor.com/en-us/products/iphone-15") == (
        "competitor.com/en-us/products/*"
    )


def test_pattern_ordinary_short_slug_is_not_mistaken_for_id() -> None:
    # "iphone-15" is 9 chars and contains both a letter and a digit, but
    # it's hyphenated (not a contiguous alnum run) and its digit-ratio
    # (2/9) is well under 0.5 -- it must be kept literally, not become
    # ":id", when it is NOT immediately after a known product key.
    assert derive_url_pattern("https://competitor.com/shop/iphone-15") == (
        "competitor.com/shop/iphone-15"
    )


def test_pattern_long_mixed_alphanumeric_hash_is_id() -> None:
    assert derive_url_pattern("https://competitor.com/p/9f8a7b6c") == "competitor.com/p/*"
    # Not after a product key -> the mixed-alnum id-like rule fires directly.
    assert derive_url_pattern("https://competitor.com/ref/9f8a7b6c") == "competitor.com/ref/:id"


def test_pattern_mostly_digits_segment_is_id() -> None:
    # len >= 4, digit-ratio >= 0.5 ("sku12" -> 2/5 digits = 0.4, NOT id;
    # "1a2b3c" -> len 6, digit-ratio 3/6 = 0.5 -> id-like).
    assert derive_url_pattern("https://competitor.com/x/1a2b3c") == "competitor.com/x/:id"


def test_pattern_short_non_digit_segment_is_kept() -> None:
    assert derive_url_pattern("https://competitor.com/sale") == "competitor.com/sale"


def test_pattern_worked_example_full() -> None:
    url = "https://www.Competitor.com/ar/products/iphone-15/?utm=x#frag"
    assert normalize_url(url) == "https://competitor.com/ar/products/iphone-15?utm=x"
    assert derive_url_pattern(url) == "competitor.com/ar/products/*"


# --- derive_match_url_fields --------------------------------------------


def test_derive_match_url_fields_returns_version_constant() -> None:
    normalized, pattern, version = derive_match_url_fields(
        "https://www.Competitor.com/ar/products/iphone-15/?utm=x#frag"
    )
    assert normalized == "https://competitor.com/ar/products/iphone-15?utm=x"
    assert pattern == "competitor.com/ar/products/*"
    assert version == URL_PATTERN_ALGORITHM_VERSION
    assert isinstance(URL_PATTERN_ALGORITHM_VERSION, int)
    assert URL_PATTERN_ALGORITHM_VERSION == 1
