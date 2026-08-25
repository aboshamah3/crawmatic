"""Unit tests for `scrape_core.browser.domain_profile_registry` (EPA B6b).

Pure in-process dict logic -- no Playwright/Chromium, no DB. Covers the
seam `scrape_core.browser.ssrf.abort_unsafe_request` relies on to
recover a real per-domain profile it is never handed directly (see that
module's docstring for why a registry, not a direct argument, is the
only available mechanism given scrapy-playwright's single-argument
``PLAYWRIGHT_ABORT_REQUEST`` calling convention).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scrape_core.browser.domain_profile_registry import (
    clear_domain_profile,
    get_domain_profile,
    set_domain_profile,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    # The module-level dict is process-global (module docstring: no TTL,
    # entries are overwritten never appended-only) -- reset between tests
    # so one test's hostname never leaks into another's assertions.
    yield
    clear_domain_profile("shop.example.com")
    clear_domain_profile("SHOP.EXAMPLE.COM")
    clear_domain_profile("other.example.com")


def test_unknown_hostname_returns_none():
    assert get_domain_profile("never-registered.example.com") is None


def test_set_then_get_round_trips():
    profile = SimpleNamespace(certified_resources={"xhr"})
    set_domain_profile("shop.example.com", profile)
    assert get_domain_profile("shop.example.com") is profile


def test_hostname_lookup_is_case_insensitive():
    profile = SimpleNamespace(certified_resources={"xhr"})
    set_domain_profile("Shop.Example.COM", profile)
    assert get_domain_profile("shop.example.com") is profile
    assert get_domain_profile("SHOP.EXAMPLE.COM") is profile


def test_later_set_overwrites_earlier_value_for_same_host():
    first = SimpleNamespace(certified_resources={"xhr"})
    second = SimpleNamespace(certified_resources={"image"})
    set_domain_profile("shop.example.com", first)
    set_domain_profile("shop.example.com", second)
    assert get_domain_profile("shop.example.com") is second


def test_distinct_hosts_do_not_collide():
    profile_a = SimpleNamespace(certified_resources={"xhr"})
    profile_b = SimpleNamespace(certified_resources={"image"})
    set_domain_profile("shop.example.com", profile_a)
    set_domain_profile("other.example.com", profile_b)
    assert get_domain_profile("shop.example.com") is profile_a
    assert get_domain_profile("other.example.com") is profile_b


@pytest.mark.parametrize("hostname", [None, ""])
def test_falsy_hostname_set_is_a_silent_noop(hostname):
    # Never raises -- an unparseable dispatch URL producing "" or None
    # hostname must not crash the registry.
    set_domain_profile(hostname, SimpleNamespace(certified_resources={"xhr"}))
    assert get_domain_profile(hostname) is None


@pytest.mark.parametrize("hostname", [None, ""])
def test_falsy_hostname_get_returns_none(hostname):
    assert get_domain_profile(hostname) is None


def test_clear_removes_the_entry():
    set_domain_profile("shop.example.com", SimpleNamespace(certified_resources={"xhr"}))
    clear_domain_profile("shop.example.com")
    assert get_domain_profile("shop.example.com") is None


def test_clear_unknown_hostname_is_a_silent_noop():
    clear_domain_profile("never-registered.example.com")  # must not raise


def test_registry_miss_is_the_documented_default_policy_value():
    """`None` is what `should_block`'s `domain_profile` parameter treats as
    "no certified resources for this domain" -- proves the registry's own
    miss value matches that contract exactly (fail-closed to blocking,
    never to allowing)."""
    from app_shared.profiles.browser_resource_policy import should_block

    profile = get_domain_profile("never-registered.example.com")
    assert profile is None
    assert should_block("https://never-registered.example.com/x.jpg", "image", profile)
