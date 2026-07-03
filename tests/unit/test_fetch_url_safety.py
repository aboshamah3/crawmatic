"""Fetch-time SSRF safety tests (SPEC-07 US2 T030, contracts/fetch-url-safety.md).

`validate_resolved_target` is pure (no Scrapy/Twisted, no real DNS) —
every case here drives it through an injected fake `resolver` callable,
never a real network lookup (FR-021/SC-007: no real-competitor network
calls in tests).
"""

from __future__ import annotations

import socket

import pytest

from app_shared.url_safety import UnsafeUrlError, UnsafeUrlReason
from scrape_core.safety.fetch import validate_resolved_target

_PUBLIC_IP = "93.184.216.34"  # a real, globally-routable IPv4 address used only as fixture data --
# never actually connected to in this test (the injected resolver is a
# fake returning a canned string; no socket is ever opened).


def _resolver(ips: list[str]):
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return ips

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


# --- accept: injected public IP -----------------------------------------------


def test_public_resolved_ip_is_accepted() -> None:
    resolver = _resolver([_PUBLIC_IP])

    validate_resolved_target("https://shop.example.com/product/1", resolver=resolver)

    assert resolver.calls == ["shop.example.com"]  # type: ignore[attr-defined]


# --- deny: private/loopback/link-local/unique-local/metadata resolved IP ------


@pytest.mark.parametrize(
    "label,resolved_ip",
    [
        ("private", "10.0.0.5"),
        ("loopback", "127.0.0.1"),
        ("link_local", "169.254.1.1"),
        ("unique_local_ipv6", "fd00::1"),
        ("cloud_metadata", "169.254.169.254"),
    ],
)
def test_unsafe_resolved_ip_is_rejected(label: str, resolved_ip: str) -> None:
    resolver = _resolver([resolved_ip])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("https://shop.example.com/product/1", resolver=resolver)

    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP, label


def test_unsafe_resolved_ip_accepted_when_explicitly_allowlisted() -> None:
    """The loopback-fixture-server seam: an explicit allowlist overrides the deny rule."""
    resolver = _resolver(["127.0.0.1"])

    validate_resolved_target(
        "https://shop.example.com/product/1",
        resolver=resolver,
        allowlist=["127.0.0.1"],
    )


# --- each redirect hop is independently re-validated ---------------------------


def test_each_redirect_hop_is_independently_revalidated() -> None:
    """Simulates the middleware's per-hop call pattern: one call per hop,
    each validated on its own merits -- a safe first hop does not grant
    the next hop a pass."""
    hop1_resolver = _resolver([_PUBLIC_IP])
    validate_resolved_target("https://shop.example.com/", resolver=hop1_resolver)

    hop2_resolver = _resolver(["10.0.0.5"])  # public host 302s to an internal IP
    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target(
            "https://shop.example.com/redirected-internal", resolver=hop2_resolver
        )
    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP


# --- scheme/userinfo rejected pre-fetch (no resolver call) ---------------------


def test_bad_scheme_rejected_before_any_resolver_call() -> None:
    resolver = _resolver([_PUBLIC_IP])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("ftp://shop.example.com/file", resolver=resolver)

    assert exc_info.value.reason == UnsafeUrlReason.BAD_SCHEME
    assert resolver.calls == []  # type: ignore[attr-defined]  -- no network attempted


def test_userinfo_rejected_before_any_resolver_call() -> None:
    resolver = _resolver([_PUBLIC_IP])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target(
            "https://user:pass@shop.example.com/", resolver=resolver
        )

    assert exc_info.value.reason == UnsafeUrlReason.USERINFO_PRESENT
    assert resolver.calls == []  # type: ignore[attr-defined]


def test_ip_literal_deny_rejected_before_any_resolver_call() -> None:
    """The IP-literal deny is a save-time check (host itself unsafe) --
    also rejected before the injected resolver is ever consulted."""
    resolver = _resolver([_PUBLIC_IP])

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target("http://127.0.0.1/", resolver=resolver)

    assert exc_info.value.reason == UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP
    assert resolver.calls == []  # type: ignore[attr-defined]


# --- production path: real resolver seam, no allowlist -------------------------


def _real_resolver(host: str) -> list[str]:
    """A real (non-fake) resolver -- what production wiring passes.

    Uses `socket.getaddrinfo` (the actual resolution machinery, not a
    fake), but only ever against a host that resolves purely via the
    local machine's own hosts-file entry (its own hostname), so this
    stays within the "no real network/DNS calls" test-environment
    constraint while still exercising the real resolver seam end to
    end, not a stand-in.
    """
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def test_production_path_uses_real_resolver_with_no_allowlist() -> None:
    """Prod always validates the real resolved IP with allowlist=None (default).

    The machine's own hostname resolves (via /etc/hosts, no network) to
    a loopback/private address, so a real (non-fake) resolver correctly
    denies it through the same seam production wiring uses -- proving
    the seam itself, not a fake, does the resolving here. (On the rare
    environment where the machine's hostname happens to collide with a
    save-time `INTERNAL_HOSTNAMES` entry, the save-time check rejects
    it first -- still a safe deny, just via the other layer.)
    """
    own_hostname = socket.gethostname()

    with pytest.raises(UnsafeUrlError) as exc_info:
        validate_resolved_target(f"http://{own_hostname}/", resolver=_real_resolver)

    assert exc_info.value.reason in (
        UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
        UnsafeUrlReason.INTERNAL_HOSTNAME,
    )
