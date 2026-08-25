"""Unit tests for `app_shared.profiles.browser_resource_policy` (EPA B6).

Pure, DB-independent, no Playwright/Scrapy import anywhere in this file
(`app_shared.profiles` is a scrapy/twisted/playwright-free zone,
`tests/unit/test_import_boundaries.py`). Covers the plan's Step 1
contract: default blocklist (types + ad/analytics/social host category),
`document` never blocked, `certified_resources` pass-through (tolerant
getattr, no domain-profile schema required), and the non-negotiable
ordering -- URL/redirect safety runs BEFORE the block decision, never
instead of it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app_shared.profiles import browser_resource_policy
from app_shared.profiles.browser_resource_policy import (
    BLOCKED_RESOURCE_TYPES,
    evaluate_request,
    should_block,
    split_attempt_bytes,
)


@pytest.fixture
def default_profile():
    """A domain profile with no `certified_resources` at all -- the every-domain
    default until a future certification step adds the field/enables it."""
    return SimpleNamespace()


@pytest.fixture
def profile_with_certified():
    """A domain profile that has certified `xhr` for its own pass-through."""
    return SimpleNamespace(certified_resources={"xhr"})


# --- default blocklist: resource types ---------------------------------------


def test_images_blocked_by_default(default_profile):
    assert should_block("https://m.media-amazon.com/x.jpg", "image", default_profile)


def test_media_blocked_by_default(default_profile):
    assert should_block("https://m.media-amazon.com/clip.mp4", "media", default_profile)


def test_fonts_blocked_by_default(default_profile):
    assert should_block("https://m.media-amazon.com/font.woff2", "font", default_profile)


def test_stylesheets_blocked_by_default(default_profile):
    # Carried forward from the pre-B6 `_COST_BLOCKED_RESOURCE_TYPES` guard
    # (scrape_core.browser.ssrf) -- not in the plan's Step 1 list verbatim,
    # but dropping it would have been a silent cost regression.
    assert should_block("https://m.media-amazon.com/style.css", "stylesheet", default_profile)


def test_scripts_not_blocked_by_default_on_ordinary_host(default_profile):
    assert not should_block("https://www.amazon.sa/main.js", "script", default_profile)


def test_xhr_not_blocked_by_default_on_ordinary_host(default_profile):
    assert not should_block("https://www.amazon.sa/api/price", "xhr", default_profile)


# --- default blocklist: ad/analytics/social host category --------------------


def test_ad_hosts_blocked_regardless_of_type(default_profile):
    assert should_block("https://amazon-adsystem.com/s.js", "script", default_profile)


def test_analytics_hosts_blocked_regardless_of_type(default_profile):
    assert should_block(
        "https://www.google-analytics.com/collect", "xhr", default_profile
    )


def test_social_widget_hosts_blocked_regardless_of_type(default_profile):
    assert should_block("https://connect.facebook.net/en_US/sdk.js", "script", default_profile)


def test_host_category_match_is_suffix_not_substring(default_profile):
    # A host that merely CONTAINS a blocked suffix as a substring (not a
    # real subdomain/exact match) must not be caught -- a substring match
    # would be a deny-list bypass in the other direction (over-blocking
    # an unrelated, legitimate domain).
    assert not should_block(
        "https://notgoogletagmanager.com/evil.js", "script", default_profile
    )


# --- document is never blocked ------------------------------------------------


def test_document_never_blocked(default_profile):
    assert not should_block("https://www.amazon.sa/dp/B0ABC", "document", default_profile)


def test_document_never_blocked_even_on_ad_host(default_profile):
    # Belt-and-braces: even a document-typed navigation to an ad-category
    # host is not this policy's call to make -- it is never reached
    # (should_block's `document` guard is unconditional, checked first).
    assert not should_block("https://doubleclick.net/", "document", default_profile)


# --- certified_resources pass-through -----------------------------------------


def test_certified_resource_allowed(profile_with_certified):
    assert not should_block(
        "https://noon.com/api/price.json", "xhr", profile_with_certified
    )


def test_certified_resource_overrides_type_blocklist():
    # A resource TYPE the default blocklist would otherwise catch (image)
    # is let through once certified -- proves certification is a real
    # override, not just "xhr was never blocked anyway".
    profile = SimpleNamespace(certified_resources={"image"})
    assert not should_block("https://noon.com/thumb.jpg", "image", profile)


def test_certified_resource_overrides_host_category_blocklist():
    # Certifying a type also overrides the ad/analytics/social host
    # category block for that type -- "only the profile's
    # certified_resources are allowed through" is a full override.
    profile = SimpleNamespace(certified_resources={"script"})
    assert not should_block("https://amazon-adsystem.com/s.js", "script", profile)


def test_missing_certified_resources_attribute_is_tolerant():
    # No domain-profile file needed a schema change to land this module --
    # a plain object with no `certified_resources` attribute at all must
    # behave exactly like an empty certification set, never raise.
    assert should_block("https://m.media-amazon.com/x.jpg", "image", object())


def test_none_domain_profile_is_tolerant():
    assert should_block("https://m.media-amazon.com/x.jpg", "image", None)
    assert should_block("https://m.media-amazon.com/x.jpg", "image")


# --- ordering: safety runs before the block decision --------------------------


class _SafetySpy:
    """Records whether the URL-safety check ran before `should_block`.

    `checked_before_block_decision` starts ``True`` (no ordering
    violation observed yet) and is only ever recomputed inside
    `spying_should_block` -- so a call that never reaches the block
    decision at all (the safety check aborted it first) leaves the flag
    at its true-by-default value, exactly the "safety ran, and the block
    decision it also gated was never reached out of order" outcome.
    """

    def __init__(self) -> None:
        self.safety_checked = False
        self.checked_before_block_decision = True


@pytest.fixture
def safety_spy(monkeypatch):
    spy = _SafetySpy()

    real_validate = browser_resource_policy.validate_competitor_url

    def spying_validate(url: str) -> None:
        spy.safety_checked = True
        return real_validate(url)

    def spying_should_block(url: str, resource_type: str, domain_profile=None) -> bool:
        spy.checked_before_block_decision = spy.safety_checked
        return should_block(url, resource_type, domain_profile)

    monkeypatch.setattr(browser_resource_policy, "validate_competitor_url", spying_validate)
    monkeypatch.setattr(browser_resource_policy, "should_block", spying_should_block)
    return spy


def test_blocking_runs_after_url_safety(safety_spy, default_profile):
    evaluate_request("https://169.254.169.254/x.jpg", "image", default_profile)
    assert safety_spy.checked_before_block_decision


def test_blocking_runs_after_url_safety_on_a_safe_url_that_reaches_the_decision(
    safety_spy, default_profile
):
    # Unlike the unsafe-URL case above (where should_block is never
    # reached at all), this URL is safe -- the block decision DOES run,
    # so this actually exercises "safety checked, then block decided" in
    # that order rather than being vacuously satisfied.
    evaluate_request("https://m.media-amazon.com/x.jpg", "image", default_profile)
    assert safety_spy.safety_checked
    assert safety_spy.checked_before_block_decision


def test_unsafe_url_aborted_without_reaching_block_decision(default_profile):
    # A cloud-metadata IP literal fails url_safety's PRIVATE_OR_INTERNAL_IP
    # check -- evaluate_request must abort it even though "image" is not
    # in BLOCKED_RESOURCE_TYPES on its own merits, proving the safety
    # check is not merely run first but is dispositive on its own.
    assert evaluate_request("https://169.254.169.254/x.jpg", "image", default_profile)


def test_certification_cannot_launder_an_unsafe_url():
    # Certifying "image" must never let an SSRF target through --
    # evaluate_request's safety check runs unconditionally BEFORE
    # should_block/certified_resources are ever consulted.
    profile = SimpleNamespace(certified_resources={"image"})
    assert evaluate_request("https://169.254.169.254/x.jpg", "image", profile)


def test_safe_url_reaches_the_normal_block_decision(default_profile):
    assert evaluate_request("https://m.media-amazon.com/x.jpg", "image", default_profile)
    assert not evaluate_request("https://www.amazon.sa/dp/B0ABC", "document", default_profile)


# --- byte accounting split -----------------------------------------------------


def test_split_attempt_bytes_separates_document_from_subresources():
    observed = [
        ("document", 1_300_000),
        ("image", 4_000_000),
        ("script", 100_000),
        ("media", 2_050_000),
    ]
    main_document_bytes, subresource_bytes = split_attempt_bytes(observed)
    assert main_document_bytes == 1_300_000
    assert subresource_bytes == 4_000_000 + 100_000 + 2_050_000


def test_split_attempt_bytes_empty_observations_is_zero_not_none():
    assert split_attempt_bytes([]) == (0, 0)


def test_split_attempt_bytes_multiple_documents_sum():
    # A redirect chain can carry more than one "document" response for a
    # single attempt -- both legitimately belong to main_document_bytes.
    observed = [("document", 500), ("document", 1_300_000)]
    main_document_bytes, subresource_bytes = split_attempt_bytes(observed)
    assert main_document_bytes == 500 + 1_300_000
    assert subresource_bytes == 0
