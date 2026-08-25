"""Hostile-fixture SSRF corpus for the URL-safety bounds (READY-013-c).

`tests/unit/test_url_safety.py` is the *contract* corpus — the accept/deny
table `contracts/url-safety.md` specifies. This file is the adversarial
counterpart: every fixture here is a shape an attacker actually uses to
smuggle an internal target past a naive validator, grouped by attack
class:

* private / link-local / loopback ranges, IPv4 **and** IPv6 (incl. the
  IPv4-mapped, 6to4 and NAT64 embeddings of a private v4 address),
* the non-dotted-quad IPv4 encodings ``inet_aton``/libc accept —
  decimal (``2130706433``), hex (``0x7f000001``), octal
  (``017700000001``) and the short ``127.1`` form,
* URL-parser confusion — userinfo (``http://safe.com@169.254.169.254/``),
  backslash hosts, percent-encoded hosts, trailing-root-dot FQDNs, and
  IDNA/NFKC look-alikes that fold onto an internal name,
* DNS rebinding, through the **injected** resolver seam
  (`scrape_core.safety.fetch.validate_resolved_target`) — a host that
  answers public once and private next,
* redirect pivots — every hop re-validated on its own merits, including
  the post-redirect DNS resolution,
* scheme downgrades (``file:``/``gopher:``/``ftp:``/``dict:``…), and
* response bombs — the download bounds that cap a lying
  ``Content-Length``, an unbounded stream, and a decompression bomb.

Every fixture that the current code already blocks stays here as a
**regression pin** (no code change was needed for it); the ones that
exposed a real gap drove the fix in `app_shared.url_safety`.

No network and no real DNS: every resolution goes through an injected
fake resolver, exactly as `tests/unit/test_fetch_url_safety.py` does.
"""

from __future__ import annotations

from typing import Any

import pytest

from app_shared.url_safety import (
    UnsafeUrlError,
    UnsafeUrlReason,
    validate_competitor_url,
)
from scrape_core.safety.fetch import validate_resolved_target

# A real, globally-routable address used purely as fixture data — never
# connected to (the resolver in this file is always a fake).
_PUBLIC_IP = "93.184.216.34"


def _resolver(*answers: list[str]):
    """Fake resolver returning `answers[n]` on the n-th call.

    A single answer is returned for every call (a stable host); several
    answers model **DNS rebinding** — the same hostname resolving
    differently between the check and the connect.
    """
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        index = min(len(calls) - 1, len(answers) - 1)
        return answers[index]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


# --------------------------------------------------------------------------
# 1. Private / link-local / loopback literals — IPv4 and IPv6
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,url",
    [
        ("rfc1918_10", "http://10.0.0.1/"),
        ("rfc1918_10_high", "http://10.255.255.254/"),
        ("rfc1918_172_low", "http://172.16.0.1/"),
        ("rfc1918_172_mid", "http://172.20.10.7/"),
        ("rfc1918_172_high", "http://172.31.255.254/"),
        ("rfc1918_192_168", "http://192.168.1.1/"),
        ("loopback_v4", "http://127.0.0.1/"),
        ("loopback_v4_alt", "http://127.99.88.77/"),
        ("unspecified_v4", "http://0.0.0.0/"),
        ("this_network", "http://0.1.2.3/"),
        ("link_local_v4", "http://169.254.1.2/"),
        ("cloud_metadata_aws", "http://169.254.169.254/latest/meta-data/"),
        ("cloud_metadata_alibaba", "http://100.100.100.200/latest/meta-data/"),
        ("cgnat", "http://100.64.0.1/"),
        ("benchmarking", "http://198.18.0.1/"),
        ("ietf_protocol", "http://192.0.0.1/"),
        ("multicast_v4", "http://224.0.0.1/"),
        ("broadcast_v4", "http://255.255.255.255/"),
        ("loopback_v6", "http://[::1]/"),
        ("unspecified_v6", "http://[::]/"),
        ("link_local_v6", "http://[fe80::1]/"),
        ("link_local_v6_high", "http://[febf::dead:beef]/"),
        ("unique_local_v6_fc", "http://[fc00::1]/"),
        ("unique_local_v6_fd", "http://[fd12:3456:789a::1]/"),
        ("multicast_v6", "http://[ff02::1]/"),
        # IPv4-mapped IPv6 — the private v4 address wearing a v6 costume.
        ("v4_mapped_private", "http://[::ffff:10.0.0.1]/"),
        ("v4_mapped_loopback", "http://[::ffff:127.0.0.1]/"),
        ("v4_mapped_metadata", "http://[::ffff:169.254.169.254]/"),
        ("v4_mapped_hex_form", "http://[::ffff:a00:1]/"),
        ("v4_mapped_expanded", "http://[0:0:0:0:0:ffff:169.254.169.254]/"),
        # 6to4 / NAT64 embeddings of 127.0.0.1.
        ("sixtofour_loopback", "http://[2002:7f00:0001::]/"),
        ("nat64_loopback", "http://[64:ff9b::7f00:1]/"),
    ],
)
def test_private_and_loopback_literals_are_rejected(label: str, url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP, label


# --------------------------------------------------------------------------
# 2. Alternate IPv4 encodings — decimal / hex / octal / short form
#
# `ipaddress.ip_address` only accepts the strict dotted quad, so these
# used to slip through the IP branch entirely and be treated as ordinary
# DNS names. libc's `inet_aton` (and therefore `getaddrinfo`, and
# therefore the actual fetch) resolves every one of them to 127.0.0.1 —
# verified on this box. That was a real bypass, not a theoretical one.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,url,expected_ip",
    [
        ("decimal_loopback", "http://2130706433/", "127.0.0.1"),
        ("hex_loopback", "http://0x7f000001/", "127.0.0.1"),
        ("hex_loopback_upper", "http://0X7F000001/", "127.0.0.1"),
        ("octal_loopback", "http://017700000001/", "127.0.0.1"),
        ("dotted_octal_loopback", "http://0177.0.0.1/", "127.0.0.1"),
        ("short_form_loopback", "http://127.1/", "127.0.0.1"),
        ("two_part_loopback", "http://127.0.1/", "127.0.0.1"),
        ("decimal_metadata", "http://2852039166/", "169.254.169.254"),
        ("hex_metadata", "http://0xa9fea9fe/", "169.254.169.254"),
        ("dotted_hex_metadata", "http://0xa9.0xfe.0xa9.0xfe/", "169.254.169.254"),
        ("decimal_rfc1918", "http://3232235777/", "192.168.1.1"),
        ("mixed_octal_rfc1918", "http://012.0.0.1/", "10.0.0.1"),
        ("short_form_rfc1918", "http://10.1/", "10.0.0.1"),
    ],
)
def test_alternate_ipv4_encodings_are_decoded_and_rejected(
    label: str, url: str, expected_ip: str
) -> None:
    """A libc-decodable IPv4 literal is judged as the address it decodes to."""
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP, label


def test_alternate_encoding_of_a_public_address_is_still_accepted() -> None:
    """The decoder judges the *address*, not the notation — no false deny.

    `1560072194` is `93.0.216.66`, a public address, so the same
    normalization that rejects `2130706433` accepts this one.
    """
    assert validate_competitor_url("http://1560072194/") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://3com.com/",  # digit-leading label, not a number
        "http://0x.example.com/",
        "http://999.999.999.999/",  # out of range in every base
        "http://1.2.3.4.5/",  # too many parts for inet_aton
    ],
)
def test_hostnames_that_only_look_numeric_are_not_misread_as_ips(url: str) -> None:
    """The loose-IPv4 decoder must not swallow ordinary hostnames."""
    try:
        validate_competitor_url(url)
    except UnsafeUrlError as exc:  # pragma: no cover - documents the reason
        assert exc.reason != UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP, url


# --------------------------------------------------------------------------
# 3. URL-parser confusion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,url",
    [
        ("userinfo_host_lookalike", "http://safe.com@169.254.169.254/"),
        ("userinfo_with_password", "http://safe.com:80@10.0.0.1/"),
        ("userinfo_encoded_at", "http://169.254.169.254%23@safe.com/"),
        ("userinfo_backslash", "http://safe.com\\@169.254.169.254/"),
        ("userinfo_empty_password", "http://user:@10.0.0.1/"),
    ],
)
def test_userinfo_tricks_are_rejected(label: str, url: str) -> None:
    """Embedded credentials are refused outright — the host after `@` is
    never trusted, and the *pre*-`@` text is never mistaken for the host."""
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason in (
        UnsafeUrlReason.USERINFO_PRESENT,
        UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
    ), label


@pytest.mark.parametrize(
    "label,url",
    [
        # WHATWG treats `\` as `/`: a browser/`curl` reads the host of
        # `http://10.0.0.1\.evil.com/` as `10.0.0.1`, while `urlsplit`
        # reads the whole string as one hostname. Disagreeing parsers are
        # exactly the SSRF primitive, so a backslash host is refused.
        ("backslash_private_prefix", "http://10.0.0.1\\.evil.com/"),
        ("backslash_metadata_prefix", "http://169.254.169.254\\.evil.com/"),
        ("backslash_suffix", "https://safe.com\\.evil.com/"),
        # Percent-encoding in the authority: `urlsplit` does not decode
        # it, a percent-decoding client does — `%6c%6f` is `lo`.
        ("percent_encoded_localhost", "http://%6c%6fcalhost/"),
        ("percent_encoded_dot", "http://localhost%2e/"),
        ("percent_encoded_metadata", "http://169%2e254%2e169%2e254/"),
        # Raw separators / control characters that no legitimate host has.
        ("space_in_host", "http://10.0.0.1 .evil.com/"),
        ("null_byte_in_host", "http://evil.com%00.10.0.0.1/"),
    ],
)
def test_parser_confusion_hosts_are_rejected(label: str, url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason in (
        UnsafeUrlReason.INVALID_URL,
        UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
        UnsafeUrlReason.INTERNAL_HOSTNAME,
    ), label


@pytest.mark.parametrize(
    "label,url,expected",
    [
        # A trailing root dot is a fully-qualified name that resolves
        # identically — `localhost.` is `localhost` to every resolver.
        ("trailing_dot_localhost", "http://localhost./", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("trailing_dot_localhost_upper", "http://LOCALHOST./", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("trailing_dot_metadata", "http://metadata.google.internal./", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("trailing_dot_suffix", "http://svc.railway.internal./", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("trailing_dot_ip", "http://127.0.0.1./", UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
        ("trailing_dot_decimal_ip", "http://2130706433./", UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
    ],
)
def test_trailing_root_dot_does_not_bypass_the_deny_lists(
    label: str, url: str, expected: UnsafeUrlReason
) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == expected, label


@pytest.mark.parametrize(
    "label,url,expected",
    [
        # IDNA/nameprep NFKC-folds these onto plain ASCII before the
        # resolver ever sees them — verified: each resolves to 127.0.0.1
        # through `getaddrinfo` on this box.
        ("circled_l_localhost", "http://ⓛocalhost/", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("ideographic_stop_localhost", "http://localhost。/", UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("ideographic_stop_ip", "http://127。0。0。1/", UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
        ("fullwidth_digits_ip", "http://１２７.0.0.1/", UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
        ("fullwidth_decimal_ip", "http://２１３０７０６４３３/", UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
    ],
)
def test_idna_lookalike_hosts_are_folded_before_the_deny_lists(
    label: str, url: str, expected: UnsafeUrlReason
) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == expected, label


def test_ordinary_internationalized_domain_still_accepted() -> None:
    """Folding must not turn every IDN into a rejection — only the
    ones that fold onto an internal name/IP."""
    assert validate_competitor_url("https://フ.example.com/p/1") is None


# --------------------------------------------------------------------------
# 4. Scheme downgrades
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://localhost/etc/shadow",
        "gopher://evil.com:70/_GET%20/",
        "gopher://127.0.0.1:6379/_SET%20k%20v",
        "ftp://competitor.com/pub/",
        "ftps://competitor.com/pub/",
        "dict://127.0.0.1:11211/stat",
        "ldap://127.0.0.1:389/",
        "jar:http://evil.com!/",
        "netdoc:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "blob:https://evil.com/uuid",
        "ws://127.0.0.1/",
        "wss://competitor.com/",
    ],
)
def test_scheme_downgrades_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.BAD_SCHEME


def test_scheme_check_is_case_insensitive_both_ways() -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url("FILE:///etc/passwd")
    assert exc_info.value.reason == UnsafeUrlReason.BAD_SCHEME
    assert validate_competitor_url("HTTPS://competitor.com/x") is None


# --------------------------------------------------------------------------
# 5. DNS rebinding — through the injected resolver seam
# --------------------------------------------------------------------------


def test_dns_rebinding_second_answer_is_rejected() -> None:
    """Public on the first lookup, private on the second.

    `validate_resolved_target` judges *the answer it is given*, so the
    rebound (private) answer is refused even though the same hostname
    passed moments earlier. This is the unit-level statement of the
    property `safety.resolver.SafeResolver` enforces at connect time:
    the address actually connected to is the one that gets validated.
    """
    resolver = _resolver([_PUBLIC_IP], ["169.254.169.254"])

    validate_resolved_target("https://rebind.example.com/p/1", resolver=resolver)

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://rebind.example.com/p/1", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP


def test_rebinding_via_multi_answer_rrset_is_rejected() -> None:
    """A single RRset mixing a public and a private A record.

    Round-robin between them is rebinding without the second lookup —
    so *every* address in the answer must clear the bar, not just one.
    """
    resolver = _resolver([_PUBLIC_IP, "10.0.0.5"])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://split.example.com/p/1", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP


def test_rebinding_to_ipv6_loopback_is_rejected() -> None:
    resolver = _resolver([_PUBLIC_IP], ["::1"])

    validate_resolved_target("https://rebind6.example.com/", resolver=resolver)
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://rebind6.example.com/", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP


def test_resolver_answering_a_nonsense_address_is_rejected() -> None:
    resolver = _resolver(["not-an-ip"])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://evil.example.com/", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.INVALID_URL


def test_empty_resolver_answer_is_rejected() -> None:
    resolver = _resolver([])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://void.example.com/", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.INVALID_URL


def test_alternate_encoding_host_is_rejected_before_any_resolver_call() -> None:
    """The decimal-IP bypass must die at the save-time layer, so the
    fetch-time layer never even resolves it."""
    resolver = _resolver([_PUBLIC_IP])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("http://2130706433/", resolver=resolver)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP
    assert resolver.calls == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 6. Redirect pivots — every hop, and every hop's DNS answer
# --------------------------------------------------------------------------


_REDIRECT_CHAIN_PIVOT_ON_HOP_3 = [
    ("https://shop.example.com/p/1", [_PUBLIC_IP]),
    ("https://cdn.example.com/p/1", [_PUBLIC_IP]),
    ("https://internal.example.com/p/1", ["10.0.0.5"]),
    ("https://never.example.com/p/1", [_PUBLIC_IP]),
]


def test_redirect_pivot_to_private_is_caught_on_the_pivoting_hop() -> None:
    """Public → public → private: hops 1-2 pass, hop 3 is refused, and
    the chain stops there (hop 4 is never validated, i.e. never fetched)."""
    validated: list[str] = []

    with pytest.raises(UnsafeUrlError) as exc_info:
        for url, answer in _REDIRECT_CHAIN_PIVOT_ON_HOP_3:
            validate_resolved_target(url, resolver=_resolver(answer))
            validated.append(url)

    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP
    assert validated == [
        "https://shop.example.com/p/1",
        "https://cdn.example.com/p/1",
    ]


@pytest.mark.parametrize(
    "label,hop2_url,hop2_answer,expected",
    [
        ("pivot_to_metadata_literal", "http://169.254.169.254/latest/meta-data/", [_PUBLIC_IP], UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
        ("pivot_to_decimal_literal", "http://2130706433/", [_PUBLIC_IP], UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
        ("pivot_to_internal_hostname", "http://redis/", [_PUBLIC_IP], UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("pivot_to_trailing_dot_internal", "http://localhost./", [_PUBLIC_IP], UnsafeUrlReason.INTERNAL_HOSTNAME),
        ("pivot_to_file_scheme", "file:///etc/passwd", [_PUBLIC_IP], UnsafeUrlReason.BAD_SCHEME),
        ("pivot_to_gopher_scheme", "gopher://127.0.0.1:6379/_x", [_PUBLIC_IP], UnsafeUrlReason.BAD_SCHEME),
        ("pivot_to_userinfo", "http://safe.com@10.0.0.1/", [_PUBLIC_IP], UnsafeUrlReason.USERINFO_PRESENT),
        # The pivot the URL alone cannot show: a public *name* whose DNS
        # answer on this hop is internal.
        ("pivot_via_dns_only", "https://cdn.example.com/p/1", ["192.168.1.10"], UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP),
    ],
)
def test_second_hop_is_revalidated_from_scratch(
    label: str, hop2_url: str, hop2_answer: list[str], expected: UnsafeUrlReason
) -> None:
    """A safe hop 1 grants hop 2 nothing — URL *and* DNS are re-checked."""
    validate_resolved_target(
        "https://shop.example.com/p/1", resolver=_resolver([_PUBLIC_IP])
    )

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target(hop2_url, resolver=_resolver(hop2_answer))
    assert exc_info.value.reason == expected, label


def test_post_redirect_dns_is_resolved_again_not_reused() -> None:
    """The hop-2 host is resolved on hop 2 — a cached hop-1 answer is
    never what hop 2 is judged on (that is the rebinding hole)."""
    hop1 = _resolver([_PUBLIC_IP])
    hop2 = _resolver(["10.0.0.5"])

    validate_resolved_target("https://shop.example.com/", resolver=hop1)
    with pytest.raises(UnsafeUrlError):
        validate_resolved_target("https://shop.example.com/next", resolver=hop2)

    # Same hostname, resolved once per hop — not once per chain.
    assert hop1.calls == ["shop.example.com"]  # type: ignore[attr-defined]
    assert hop2.calls == ["shop.example.com"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 7. Response bombs — the download bounds that cap a hostile body
#
# A validator that lets the *target* through safely is still one lying
# `Content-Length`, one never-ending chunked stream, or one 1000:1 gzip
# bomb away from taking the spider process down. Scrapy enforces all
# three from `DOWNLOAD_MAXSIZE`, but only if it is actually set — its
# default is 1 GiB, which is "unbounded" for a price scraper.
# --------------------------------------------------------------------------


_SCRAPY_PROJECT_MODULES = ["price_monitor.settings", "price_monitor_browser.settings"]

_SETTINGS_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@pgbouncer:6432/db",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "scrapyd",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


@pytest.fixture
def scrapy_project_settings(monkeypatch: Any):
    """Import a Scrapy project's settings module with the env it needs.

    The settings modules read `app_shared.config.get_settings()` at
    import time (Principle IV — no hardcoded literals), so a unit run
    needs the required env present and the settings cache cleared. The
    module is loaded under a throwaway name each time so nothing this
    fixture imports leaks into another test's `sys.modules`.
    """
    import importlib.util

    from app_shared.config import get_settings

    for name, value in _SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    def load(module_name: str) -> dict[str, Any]:
        spec = importlib.util.find_spec(module_name)
        assert spec is not None and spec.loader is not None, module_name
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {name: getattr(module, name) for name in dir(module) if name.isupper()}

    yield load
    get_settings.cache_clear()


@pytest.mark.parametrize("module_name", _SCRAPY_PROJECT_MODULES)
def test_download_size_bounds_are_set_on_every_scrapy_project(
    module_name: str, scrapy_project_settings: Any
) -> None:
    settings = scrapy_project_settings(module_name)

    assert "DOWNLOAD_MAXSIZE" in settings, f"{module_name} leaves DOWNLOAD_MAXSIZE at Scrapy's 1 GiB default"
    assert "DOWNLOAD_WARNSIZE" in settings, module_name

    maxsize = settings["DOWNLOAD_MAXSIZE"]
    warnsize = settings["DOWNLOAD_WARNSIZE"]
    assert isinstance(maxsize, int) and maxsize > 0, module_name
    assert isinstance(warnsize, int) and 0 < warnsize <= maxsize, module_name
    # A product page is measured in hundreds of KB; anything near
    # Scrapy's 1 GiB default is not a bound at all.
    assert maxsize <= 64 * 1024 * 1024, f"{module_name}: DOWNLOAD_MAXSIZE {maxsize} is not a real cap"


@pytest.mark.parametrize("module_name", _SCRAPY_PROJECT_MODULES)
def test_download_timeout_is_bounded(
    module_name: str, scrapy_project_settings: Any
) -> None:
    """An unbounded *stream* is also an unbounded *time* — a slow-loris
    body needs a wall clock, not only a byte cap."""
    settings = scrapy_project_settings(module_name)

    assert "DOWNLOAD_TIMEOUT" in settings, module_name
    assert 0 < settings["DOWNLOAD_TIMEOUT"] <= 180, module_name


def _gzip_bomb(decompressed_size: int) -> bytes:
    import gzip

    return gzip.compress(b"\0" * decompressed_size)


def test_decompression_bomb_is_aborted_mid_inflate() -> None:
    """A small gzip body that inflates past `DOWNLOAD_MAXSIZE` is refused.

    Behavioral, not a source-string paraphrase: a real
    `HttpCompressionMiddleware` built from a real crawler is handed a
    real gzip bomb, and must raise rather than materialize it.
    """
    from scrapy.downloadermiddlewares.httpcompression import HttpCompressionMiddleware
    from scrapy.exceptions import IgnoreRequest
    from scrapy.http import Request, Response
    from scrapy.utils.test import get_crawler

    max_size = 64 * 1024
    crawler = get_crawler(settings_dict={"DOWNLOAD_MAXSIZE": max_size})
    middleware = HttpCompressionMiddleware.from_crawler(crawler)

    bomb = _gzip_bomb(max_size * 64)
    # The point of a bomb: the body Scrapy actually downloads is far
    # *under* the cap, and only inflating it crosses the line.
    assert len(bomb) < max_size, "the fixture must be a small body that inflates hugely"

    request = Request("https://competitor.com/p/1")
    response = Response(
        request.url,
        body=bomb,
        headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
    )

    with pytest.raises(IgnoreRequest) as exc_info:
        middleware.process_response(request, response)
    assert "DOWNLOAD_MAXSIZE" in str(exc_info.value)


def test_compressed_body_under_the_bound_still_decodes() -> None:
    """The bomb guard must not break ordinary gzipped product pages."""
    import gzip

    from scrapy.downloadermiddlewares.httpcompression import HttpCompressionMiddleware
    from scrapy.http import Request, Response
    from scrapy.utils.test import get_crawler

    crawler = get_crawler(settings_dict={"DOWNLOAD_MAXSIZE": 64 * 1024})
    middleware = HttpCompressionMiddleware.from_crawler(crawler)

    request = Request("https://competitor.com/p/1")
    response = Response(
        request.url,
        body=gzip.compress(b"<html><body>SAR 1.00</body></html>"),
        headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
    )

    decoded = middleware.process_response(request, response)
    assert b"SAR 1.00" in decoded.body


def test_content_length_lie_is_capped_by_the_streamed_byte_count() -> None:
    """A body that exceeds the cap is cut off *as it streams*.

    This is the `Content-Length`-lie / never-ending-chunked-stream case:
    the declared length is irrelevant because `_ResponseReader` counts
    the bytes actually received and cancels the download the moment they
    cross `DOWNLOAD_MAXSIZE`. Driven against the real reader with a fake
    transport — no socket, no reactor.
    """
    from scrapy.core.downloader.handlers.http11 import _ResponseReader
    from scrapy.http import Request
    from scrapy.utils.test import get_crawler
    from twisted.internet.defer import Deferred

    max_size = 1024
    crawler = get_crawler()
    crawler.spider = None

    class _FakeTransport:
        def __init__(self) -> None:
            self.stopped = False
            self.closed = False

        def stopProducing(self) -> None:
            self.stopped = True

        def loseConnection(self) -> None:
            self.closed = True

    class _FakeTxResponse:
        # A `Content-Length` that lies: it claims a tiny body.
        length = 10
        code = 200
        version = ("HTTP", 1, 1)
        headers: Any = {}

    finished: Deferred = Deferred()
    finished.addErrback(lambda failure: failure.check(Exception))

    reader = _ResponseReader(
        finished=finished,
        txresponse=_FakeTxResponse(),
        request=Request("https://competitor.com/p/1"),
        maxsize=max_size,
        warnsize=max_size // 2,
        fail_on_dataloss=False,
        crawler=crawler,
    )
    reader.transport = _FakeTransport()  # type: ignore[assignment]

    # Under the cap: the download is still live.
    reader.dataReceived(b"a" * (max_size // 2))
    assert not finished.called

    # Past the cap — despite Content-Length having promised 10 bytes.
    reader.dataReceived(b"a" * max_size)
    assert finished.called, "the streamed-byte cap did not cancel the download"


@pytest.mark.parametrize("module_name", _SCRAPY_PROJECT_MODULES)
def test_compression_middleware_is_enabled_so_its_bound_applies(
    module_name: str, scrapy_project_settings: Any
) -> None:
    """The decompression cap only exists if the middleware is in the
    chain — an explicit `None` in `DOWNLOADER_MIDDLEWARES` would disable
    both the middleware and its bomb guard."""
    settings = scrapy_project_settings(module_name)
    middlewares = settings.get("DOWNLOADER_MIDDLEWARES", {})

    disabled = [
        name
        for name, order in middlewares.items()
        if order is None and "httpcompression" in name.lower()
    ]
    assert disabled == [], f"{module_name} disables the decompression bound: {disabled}"


# --------------------------------------------------------------------------
# 8. Content-type confusion
# --------------------------------------------------------------------------


def test_non_http_scheme_cannot_be_smuggled_through_a_content_type() -> None:
    """The scheme allow-list is the only thing that decides which
    protocol is spoken — a `data:`/`file:` payload dressed as HTML is
    still rejected on its scheme, before any body is looked at."""
    for url in (
        "data:text/html,<html><body>SAR 1.00</body></html>",
        "file:///proc/self/environ",
    ):
        with pytest.raises(UnsafeUrlError) as exc_info:
            validate_competitor_url(url)
        assert exc_info.value.reason == UnsafeUrlReason.BAD_SCHEME


def test_url_path_and_query_never_influence_the_host_decision() -> None:
    """Attacker-controlled path/query/fragment must not be able to move
    the host — the deny decision is made on the authority alone."""
    assert (
        validate_competitor_url(
            "https://competitor.com/p?next=http://169.254.169.254/&x=file:///etc/passwd"
        )
        is None
    )
    assert validate_competitor_url("https://competitor.com/p#@169.254.169.254/") is None


# --------------------------------------------------------------------------
# 9. Public API stability — callers (scrapers, W3.2) depend on this shape
# --------------------------------------------------------------------------


def test_public_api_shape_is_unchanged() -> None:
    import app_shared.url_safety as mod

    assert callable(mod.validate_competitor_url)
    assert issubclass(mod.UnsafeUrlError, ValueError)
    # The reason vocabulary routers map to `UNSAFE_URL` responses.
    assert {
        "INVALID_URL",
        "BAD_SCHEME",
        "USERINFO_PRESENT",
        "PRIVATE_OR_INTERNAL_IP",
        "INTERNAL_HOSTNAME",
    } <= {reason.value for reason in mod.UnsafeUrlReason}
    assert mod.validate_competitor_url("https://competitor.com/p/1") is None


def test_still_performs_no_dns_resolution() -> None:
    """The save-time validator stays pure — the fixes above are all
    normalization, never a lookup."""
    import app_shared.url_safety as mod

    with open(mod.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert "import socket" not in text
    assert "getaddrinfo" not in text
