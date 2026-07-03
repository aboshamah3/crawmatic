"""Unit tests for `app_shared.url_safety` (T015, US2, FR-007/008, SC-004).

Pure, DB-independent — the exhaustive save-time SSRF accept/deny corpus
per `contracts/url-safety.md`. Covers both IPv4 and IPv6 forms.
"""

from __future__ import annotations

import pytest

from app_shared.url_safety import (
    INTERNAL_HOST_SUFFIXES,
    INTERNAL_HOSTNAMES,
    UnsafeUrlError,
    UnsafeUrlReason,
    validate_competitor_url,
)


# --- accept ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://competitor.com/p/123",
        "http://shop.example.co.uk/ar/products/x",
        "https://93.184.216.34/x",  # public IPv4 literal
        "https://[2606:2800:220:1:248:1893:25c8:1946]/x",  # public IPv6 literal
        "http://example.com/",
        "https://example.com:443/x",  # default port, still fine
    ],
)
def test_accepts_safe_public_urls(url: str) -> None:
    assert validate_competitor_url(url) is None


# --- reject: bad scheme --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://competitor.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.BAD_SCHEME


# --- reject: invalid / relative -------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "//competitor.com/x",  # no scheme (protocol-relative)
        "http://",  # no host
    ],
)
def test_rejects_invalid_or_relative_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.INVALID_URL


# --- reject: userinfo ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@competitor.com/",
        "https://user@competitor.com/",
    ],
)
def test_rejects_embedded_credentials(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.USERINFO_PRESENT


# --- reject: private/internal IP literals (IPv4 + IPv6) -------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://169.254.1.2/",  # link-local
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # loopback
        "http://[fe80::1]/",  # link-local
        "http://[fc00::1]/",  # unique-local
        "http://[fd12:3456:789a::1]/",  # unique-local
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata
        "http://[::]/",  # unspecified
    ],
)
def test_rejects_private_or_internal_ip_literals(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP


# --- reject: internal hostnames + suffixes ---------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://postgres:5432/",
        "http://redis/",
        "http://pgbouncer/",
        "http://api/",
        "http://scheduler/",
        "http://worker/",
        "http://scrapyd-http/",
        "http://scrapyd-browser/",
        "http://metadata.google.internal/",
        "http://api.internal/",
        "http://foo.local/",
        "http://x.railway.internal/",
        "http://foo.localhost/",
    ],
)
def test_rejects_internal_hostnames_and_suffixes(url: str) -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url(url)
    assert exc_info.value.reason == UnsafeUrlReason.INTERNAL_HOSTNAME


def test_internal_hostname_check_is_case_insensitive() -> None:
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_competitor_url("http://LOCALHOST/")
    assert exc_info.value.reason == UnsafeUrlReason.INTERNAL_HOSTNAME


# --- deny-list constants match the contract exactly ------------------------


def test_internal_hostnames_constant_matches_contract() -> None:
    assert INTERNAL_HOSTNAMES == {
        "localhost",
        "postgres",
        "redis",
        "pgbouncer",
        "api",
        "scheduler",
        "worker",
        "scrapyd-http",
        "scrapyd-browser",
        "metadata.google.internal",
    }


def test_internal_host_suffixes_constant_matches_contract() -> None:
    assert INTERNAL_HOST_SUFFIXES == (".localhost", ".local", ".internal", ".railway.internal")


# --- no DNS resolution: module never imports socket/dns -------------------


def test_url_safety_module_has_no_network_io_imports() -> None:
    import app_shared.url_safety as mod

    source = mod.__file__
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "import socket" not in text
    assert "getaddrinfo" not in text
