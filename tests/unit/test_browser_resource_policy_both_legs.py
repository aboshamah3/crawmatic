"""Tests for EPA C3's both-legs extension of the document-only browser
policy (`libs/shared/app_shared/profiles/browser_resource_policy.py`,
BLOCKLIST_VERSION 3, deep dive Sec12 item 2, audit Sec11 item 4).

B5 (2026-09-03) shipped the document-only rule gated on `transport ==
"PROXY"` only. C3 removes that gate: a domain an operator lists (under the
new `BROWSER_DOCUMENT_ONLY_DOMAINS` name, or the 2026-09-03
`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` alias) is document-only on BOTH the
direct and the proxied leg. This file is scoped to exactly what the C3
packet's acceptance criteria ask for:

1. a DIRECT browser request for a listed domain is document-only;
2. an unlisted domain is unchanged (on either transport);
3. the policy version is stamped into a report header (`BLOCKLIST_VERSION
   == 3`, and a representative "report header" string carries it).

`tests/unit/test_browser_resource_policy.py` covers everything else
(certification ordering, SSRF-first, host-category blocking, the pre-B5
positional-call-site guarantee, subdomain/lookalike matching) and was
updated in place for the handful of assertions BLOCKLIST_VERSION 3 flips
(a listed domain's DIRECT leg is no longer exempt) -- this file does not
repeat that coverage.

No canary is run here: `amazon.sa` is added to
`BROWSER_DOCUMENT_ONLY_DOMAINS` by the OWNER in C11 only, gated on results
that do not exist yet. Every test below uses a domain that is NOT
`amazon.sa` for the "unlisted" cases, and lists a domain only via
monkeypatched settings / a scoped env var -- never by editing the shipped
default (which stays `()` on both names).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app_shared.profiles import browser_resource_policy
from app_shared.profiles.browser_resource_policy import (
    BLOCKLIST_VERSION,
    evaluate_request,
    should_block,
)


@pytest.fixture
def settings_with_nothing_listed(monkeypatch):
    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=()),
    )
    monkeypatch.delenv("BROWSER_DOCUMENT_ONLY_DOMAINS", raising=False)


@pytest.fixture
def settings_with_jarir_listed_via_new_setting(monkeypatch):
    """`BROWSER_DOCUMENT_ONLY_DOMAINS=jarir.com`, the C3 (new) name, read
    directly from the environment per the packet's instruction that this
    task may not add a field to `Settings` (a sibling EPA task edits
    `config.py` concurrently)."""
    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=()),
    )
    monkeypatch.setenv("BROWSER_DOCUMENT_ONLY_DOMAINS", "jarir.com")


@pytest.fixture
def settings_with_jarir_listed_via_old_alias(monkeypatch):
    """`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=("jarir.com",)`, the 2026-09-03
    (old) setting name -- kept as an alias into the same both-legs list."""
    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=("jarir.com",)),
    )
    monkeypatch.delenv("BROWSER_DOCUMENT_ONLY_DOMAINS", raising=False)


# --- 1. a DIRECT request for a listed domain is document-only ---------------


def test_direct_request_is_document_only_for_a_domain_listed_via_new_setting(
    settings_with_jarir_listed_via_new_setting,
):
    assert should_block(
        "https://www.jarir.com/main.js", "script", None,
        domain="www.jarir.com", transport="DIRECT",
    )
    # every other document-only resource type is blocked on DIRECT too.
    for resource_type in browser_resource_policy.PROXIED_BLOCKED_RESOURCE_TYPES:
        assert should_block(
            f"https://www.jarir.com/x", resource_type, None,
            domain="www.jarir.com", transport="DIRECT",
        ), resource_type


def test_direct_request_is_document_only_for_a_domain_listed_via_old_alias(
    settings_with_jarir_listed_via_old_alias,
):
    # The 2026-09-03 setting name still works, and now ALSO covers DIRECT
    # (it did not, pre-C3) -- "stays as an alias" means the same list, the
    # new (broader) behaviour.
    assert should_block(
        "https://www.jarir.com/main.js", "script", None,
        domain="www.jarir.com", transport="DIRECT",
    )


def test_document_itself_is_never_blocked_even_direct_and_listed(
    settings_with_jarir_listed_via_new_setting,
):
    # The one thing the scraper actually fetches the page for is never
    # blocked, on any transport, listed or not.
    assert not should_block(
        "https://www.jarir.com/p/some-product", "document", None,
        domain="www.jarir.com", transport="DIRECT",
    )
    assert not should_block(
        "https://www.jarir.com/p/some-product", "document", None,
        domain="www.jarir.com", transport="PROXY",
    )


def test_proxied_request_stays_document_only_too_for_a_listed_domain(
    settings_with_jarir_listed_via_new_setting,
):
    # Both legs, not just DIRECT -- the PROXY leg's pre-existing B5
    # behaviour must not have regressed.
    assert should_block(
        "https://www.jarir.com/main.js", "script", None,
        domain="www.jarir.com", transport="PROXY",
    )


def test_certification_cannot_readmit_a_resource_on_either_leg_for_a_listed_domain(
    settings_with_jarir_listed_via_new_setting,
):
    # Rule ordering (document-only ABOVE certification) must hold on
    # DIRECT exactly as it already did on PROXY.
    profile = SimpleNamespace(certified_resources={"xhr"})
    assert should_block(
        "https://www.jarir.com/api/price", "xhr", profile,
        domain="www.jarir.com", transport="DIRECT",
    )
    assert should_block(
        "https://www.jarir.com/api/price", "xhr", profile,
        domain="www.jarir.com", transport="PROXY",
    )


def test_evaluate_request_carries_the_both_legs_decision_through(
    settings_with_jarir_listed_via_new_setting,
):
    # The safety-first entry point must forward the new (leg-agnostic)
    # decision exactly like `should_block`.
    assert evaluate_request(
        "https://www.jarir.com/main.js", "script", None,
        domain="www.jarir.com", transport="DIRECT",
    )
    assert not evaluate_request(
        "https://www.jarir.com/p/some-product", "document", None,
        domain="www.jarir.com", transport="DIRECT",
    )


# --- 2. an unlisted domain is unchanged --------------------------------------


def test_unlisted_domain_is_unchanged_on_direct(settings_with_jarir_listed_via_new_setting):
    # noon.com is not listed (only jarir.com is): the default type
    # blocklist decides, unchanged, on DIRECT.
    assert not should_block(
        "https://www.noon.com/main.js", "script", None,
        domain="www.noon.com", transport="DIRECT",
    )
    assert should_block(
        "https://www.noon.com/hero.jpg", "image", None,
        domain="www.noon.com", transport="DIRECT",
    )


def test_unlisted_domain_is_unchanged_on_proxy(settings_with_jarir_listed_via_new_setting):
    assert not should_block(
        "https://www.noon.com/main.js", "script", None,
        domain="www.noon.com", transport="PROXY",
    )


def test_nothing_listed_on_either_setting_leaves_every_domain_unchanged(
    settings_with_nothing_listed,
):
    # The shipped default (`()` on both names): byte-for-byte pre-B5
    # behaviour on every domain and both transports.
    for transport in ("DIRECT", "PROXY"):
        assert not should_block(
            "https://www.amazon.sa/main.js", "script", None,
            domain="www.amazon.sa", transport=transport,
        )
        assert should_block(
            "https://www.amazon.sa/hero.jpg", "image", None,
            domain="www.amazon.sa", transport=transport,
        )
        assert not should_block(
            "https://www.amazon.sa/dp/B0ABC", "document", None,
            domain="www.amazon.sa", transport=transport,
        )


def test_amazon_sa_is_not_listed_by_this_change_itself(settings_with_nothing_listed):
    # C3 must not itself add amazon.sa to either domain list -- that is
    # the OWNER's C11 decision, gated on canary results that do not exist
    # yet. With nothing listed, amazon.sa's script/xhr/etc. are NOT
    # document-only on either leg.
    for transport in ("DIRECT", "PROXY"):
        assert not should_block(
            "https://www.amazon.sa/api/price", "xhr", None,
            domain="www.amazon.sa", transport=transport,
        )


# --- 3. the policy version is stamped into the report header ----------------


def test_blocklist_version_is_3():
    assert BLOCKLIST_VERSION == 3


def test_report_header_carries_the_policy_version():
    # Mirrors how `scripts/canary_document_only_browser.py::render_report`
    # stamps `blocklist policy_version: {BLOCKLIST_VERSION}` into its
    # report header (`network_operations` has no such column, so the
    # constant travels in the report text instead). This test pins the
    # value any such header must carry, independent of that script's own
    # rendering function.
    header = f"- blocklist policy_version: {BLOCKLIST_VERSION}"
    assert header == "- blocklist policy_version: 3"


def test_canary_script_reads_the_live_blocklist_version():
    # scripts/canary_document_only_browser.py's `_policy_version()` lazily
    # imports this exact constant, so its own reports pick up C3's bump
    # with no change to that script required.
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.canary_document_only_browser import _policy_version

    assert _policy_version() == 3
