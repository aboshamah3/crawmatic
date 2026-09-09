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

    def spying_should_block(url: str, resource_type: str, domain_profile=None, **kwargs) -> bool:
        # `**kwargs` carries B5's keyword-only `domain`/`transport` through
        # unchanged -- the spy asserts ORDER, never the decision itself.
        spy.checked_before_block_decision = spy.safety_checked
        return should_block(url, resource_type, domain_profile, **kwargs)

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


# --- EPA B5/C3: document-only browser legs for LISTED domains -----------------
#
# The canary-gated rule added on 2026-09-03 (B5, PROXY-only) and extended to
# both transports on 2026-09-08 (C3, BLOCKLIST_VERSION 3). Its whole safety
# property is that it is OFF unless an operator lists a domain in
# `BROWSER_DOCUMENT_ONLY_DOMAINS` or its alias
# `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` (both default `()`). Every test
# below either pins that gate or pins the unchanged default behaviour on the
# other side of it. `tests/unit/test_browser_resource_policy_both_legs.py`
# (EPA C3) covers the both-legs extension itself; this file's own DIRECT
# cases were updated in place to match (BLOCKLIST_VERSION 3 changes what a
# listed domain's DIRECT leg does, not just what a new leg-agnostic domain
# does).


@pytest.fixture
def settings_with_amazon_listed(monkeypatch):
    """`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS == ("amazon.sa",)`.

    Patches the settings accessor the policy module itself holds (never
    the process-wide `get_settings` cache), mirroring this repo's
    `monkeypatch.setattr(<module>, "get_settings", ...)` convention --
    so no real `Settings()` (and therefore no env/`.env`) is needed.
    """
    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=("amazon.sa",)),
    )


@pytest.fixture
def settings_with_nothing_listed(monkeypatch):
    """The shipped default: the new rule can never fire."""
    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=()),
    )


def test_blocklist_version_is_3():
    # Bumped by B5 (v2) then C3 (v3, both-legs document-only, 2026-09-08)
    # so a `policy_version` stamp on a canary run's log lines is never
    # silently reinterpreted as an earlier rule-set's decision.
    assert browser_resource_policy.BLOCKLIST_VERSION == 3


def test_proxied_blocked_resource_types_never_contains_document():
    assert "document" not in browser_resource_policy.PROXIED_BLOCKED_RESOURCE_TYPES
    assert browser_resource_policy.PROXIED_BLOCKED_RESOURCE_TYPES == frozenset(
        {
            "image",
            "media",
            "font",
            "stylesheet",
            "script",
            "xhr",
            "fetch",
            "other",
            "ping",
            "websocket",
        }
    )


def test_script_blocked_on_proxy_for_listed_domain(settings_with_amazon_listed):
    assert should_block(
        "https://www.amazon.sa/main.js",
        "script",
        None,
        domain="www.amazon.sa",
        transport="PROXY",
    )


def test_script_blocked_on_direct_for_listed_domain_too(settings_with_amazon_listed):
    # EPA C3 (2026-09-08, BLOCKLIST_VERSION 3): the document-only rule now
    # applies to BOTH transports, not just PROXY -- a listed domain's
    # DIRECT leg is document-only exactly like its PROXY leg.
    assert should_block(
        "https://www.amazon.sa/main.js",
        "script",
        None,
        domain="www.amazon.sa",
        transport="DIRECT",
    )


def test_document_not_blocked_on_proxy_for_listed_domain(settings_with_amazon_listed):
    assert not should_block(
        "https://www.amazon.sa/dp/B0ABC",
        "document",
        None,
        domain="www.amazon.sa",
        transport="PROXY",
    )


def test_script_not_blocked_on_proxy_for_unlisted_domain(settings_with_amazon_listed):
    # noon.com is not listed: the DEFAULT blocklist (which never blocked
    # `script` on an ordinary host) still decides, unchanged.
    assert not should_block(
        "https://www.noon.com/main.js",
        "script",
        None,
        domain="www.noon.com",
        transport="PROXY",
    )


def test_subdomain_of_a_listed_domain_matches(settings_with_amazon_listed):
    # Suffix match on the listed registrable domain: `www.amazon.sa`
    # (and any other host under it) is the same site as `amazon.sa`.
    for host in ("amazon.sa", "www.amazon.sa", "m.www.amazon.sa"):
        assert should_block(
            f"https://{host}/main.js", "script", None, domain=host, transport="PROXY"
        ), host


def test_lookalike_domain_does_not_match_a_listed_domain(settings_with_amazon_listed):
    # Same anti-substring property the host-category blocklist already has:
    # `notamazon.sa` is a different site, never covered by `amazon.sa`.
    assert not should_block(
        "https://notamazon.sa/main.js",
        "script",
        None,
        domain="notamazon.sa",
        transport="PROXY",
    )


def test_proxy_rule_overrides_certification(settings_with_amazon_listed):
    # "document only" means document only: a per-domain certification
    # cannot re-admit a sub-resource on a proxied leg of a listed domain
    # while the canary is measuring what document-only actually costs.
    profile = SimpleNamespace(certified_resources={"xhr"})
    assert should_block(
        "https://www.amazon.sa/api/price",
        "xhr",
        profile,
        domain="www.amazon.sa",
        transport="PROXY",
    )


def test_default_empty_setting_leaves_proxied_listed_behaviour_unchanged(
    settings_with_nothing_listed,
):
    # The shipped default: byte-for-byte today's behaviour on every domain
    # and both transports -- `script` allowed, `image` blocked by the
    # pre-existing default type blocklist, `document` never blocked.
    assert not should_block(
        "https://www.amazon.sa/main.js", "script", None,
        domain="www.amazon.sa", transport="PROXY",
    )
    assert should_block(
        "https://www.amazon.sa/hero.jpg", "image", None,
        domain="www.amazon.sa", transport="PROXY",
    )
    assert not should_block(
        "https://www.amazon.sa/dp/B0ABC", "document", None,
        domain="www.amazon.sa", transport="PROXY",
    )


def test_omitting_domain_and_transport_is_the_pre_b5_decision(settings_with_amazon_listed):
    # Every pre-B5 call site (positional-only) keeps its exact decision even
    # when a domain IS listed -- the new dimension has to be passed in.
    assert not should_block("https://www.amazon.sa/main.js", "script")
    assert should_block("https://www.amazon.sa/hero.jpg", "image")


def test_settings_failure_degrades_to_the_default_policy(monkeypatch):
    # `should_block` is documented as pure and total. A misconfigured
    # process (no env, unparseable settings) must degrade to "nothing
    # listed" -- i.e. today's behaviour -- never raise into a route handler.
    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(browser_resource_policy, "get_settings", _boom)
    assert not should_block(
        "https://www.amazon.sa/main.js", "script", None,
        domain="www.amazon.sa", transport="PROXY",
    )


def test_evaluate_request_forwards_domain_and_transport(settings_with_amazon_listed):
    # The one entry point real call sites use must carry the new dimension
    # through -- safety check first, as always.
    assert evaluate_request(
        "https://www.amazon.sa/main.js", "script", None,
        domain="www.amazon.sa", transport="PROXY",
    )
    assert not evaluate_request(
        "https://www.amazon.sa/dp/B0ABC", "document", None,
        domain="www.amazon.sa", transport="PROXY",
    )
    # EPA C3 (BLOCKLIST_VERSION 3): a listed domain is document-only on
    # DIRECT too, not just PROXY.
    assert evaluate_request(
        "https://www.amazon.sa/main.js", "script", None,
        domain="www.amazon.sa", transport="DIRECT",
    )
