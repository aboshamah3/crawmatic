"""`X-Forwarded-For` is trusted only behind the declared proxy chain.

EPA W5.5-L1 item 1. The negative tests are the point: a spoofed header must
not move a caller into a fresh rate-limit bucket, and must not be able to
raise the trusted hop count by splitting itself across header lines.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app import client_ip as client_ip_module
from app.client_ip import client_ip

PROXY = "10.0.0.7"  # the socket peer, i.e. Railway's edge
CLIENT = "203.0.113.9"  # the real caller, appended by that edge
SPOOF = "198.51.100.1"  # whatever the attacker typed


def _request(*, xff=None, socket_host: str | None = PROXY):
    """A minimal Starlette-shaped request. `xff` may be a str or a list of
    header lines (repeated headers are what a splitting attack looks like)."""
    raw: list[tuple[bytes, bytes]] = []
    if isinstance(xff, str):
        raw.append((b"x-forwarded-for", xff.encode()))
    elif isinstance(xff, list):
        raw.extend((b"x-forwarded-for", v.encode()) for v in xff)
    client = SimpleNamespace(host=socket_host) if socket_host is not None else None
    return SimpleNamespace(headers=Headers(raw=raw), client=client)


# --- the declared chain -----------------------------------------------------


def test_default_hop_count_matches_the_railway_deployment() -> None:
    """One proxy in front. Changing this changes who gets rate limited."""
    assert client_ip_module.TRUSTED_PROXY_HOPS == 1


def test_one_trusted_hop_reads_the_rightmost_entry() -> None:
    """The edge appends the address it received the connection from."""
    assert client_ip(_request(xff=f"{SPOOF}, {CLIENT}"), trusted_proxy_hops=1) == CLIENT


def test_two_trusted_hops_read_the_second_entry_from_the_right() -> None:
    assert (
        client_ip(_request(xff=f"{SPOOF}, {CLIENT}, {PROXY}"), trusted_proxy_hops=2)
        == CLIENT
    )


def test_a_single_entry_chain_is_the_client_at_one_hop() -> None:
    assert client_ip(_request(xff=CLIENT), trusted_proxy_hops=1) == CLIENT


# --- NEGATIVE: a spoofed header must buy nothing ---------------------------


def test_spoofed_leading_entries_are_ignored() -> None:
    """The old code read the LEFTMOST entry — which is exactly the spoof."""
    spoofed = f"{SPOOF}, 192.0.2.5, 192.0.2.6, {CLIENT}"
    assert client_ip(_request(xff=spoofed), trusted_proxy_hops=1) == CLIENT


def test_spoofing_cannot_spread_one_caller_across_many_buckets() -> None:
    """The abuse this defends against, stated as the assertion.

    Same real client, a different invented prefix per attempt: every attempt
    must resolve to the SAME address, or the limiter counts each one in its
    own bucket and never refuses anything.
    """
    resolved = {
        client_ip(
            _request(xff=f"10.1.1.{i}, 10.2.2.{i}, {CLIENT}"), trusted_proxy_hops=1
        )
        for i in range(50)
    }
    assert resolved == {CLIENT}


def test_zero_trusted_hops_ignores_the_header_entirely() -> None:
    """A deployment reached directly must not read the header at all."""
    assert client_ip(_request(xff=f"{SPOOF}, {CLIENT}"), trusted_proxy_hops=0) == PROXY


def test_a_chain_shorter_than_the_hop_count_falls_back_to_the_socket() -> None:
    """A request that did not traverse the configured chain is not evidence.

    Taking the leftmost entry "because it is all we have" would hand the
    attacker the bucket key on any request they send with one entry.
    """
    assert client_ip(_request(xff=SPOOF), trusted_proxy_hops=2) == PROXY


def test_splitting_the_header_across_lines_does_not_add_hops() -> None:
    """Repeated headers join into one chain; the rightmost is still the edge's."""
    request = _request(xff=[SPOOF, f"192.0.2.5, {CLIENT}"])
    assert client_ip(request, trusted_proxy_hops=1) == CLIENT


def test_empty_and_whitespace_entries_are_dropped_not_counted() -> None:
    """`a, , b` must not read as a three-hop chain ending in an empty string."""
    assert client_ip(_request(xff=f"{SPOOF}, ,  , {CLIENT}"), trusted_proxy_hops=1) == CLIENT


def test_a_whitespace_only_header_is_no_header() -> None:
    assert client_ip(_request(xff="   "), trusted_proxy_hops=1) == PROXY


# --- degenerate request shapes never raise ---------------------------------


def test_no_header_uses_the_socket() -> None:
    assert client_ip(_request(), trusted_proxy_hops=1) == PROXY


def test_no_socket_and_no_header_is_a_usable_constant() -> None:
    assert client_ip(_request(socket_host=None), trusted_proxy_hops=1) == "unknown"


def test_no_socket_with_a_valid_chain_still_reads_the_chain() -> None:
    request = _request(xff=f"{SPOOF}, {CLIENT}", socket_host=None)
    assert client_ip(request, trusted_proxy_hops=1) == CLIENT


def test_module_default_is_used_when_no_override_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_ip_module, "TRUSTED_PROXY_HOPS", 0)
    assert client_ip(_request(xff=f"{SPOOF}, {CLIENT}")) == PROXY


# --- the login limiter actually uses it ------------------------------------


def test_auth_router_derives_its_rate_limit_source_from_this_module() -> None:
    """Pins the wiring: `_client_ip` must not drift back to `request.client`."""
    from app.routers import auth as auth_router

    request = _request(xff=f"{SPOOF}, {CLIENT}")
    assert auth_router._client_ip(request) == CLIENT
