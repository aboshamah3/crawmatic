"""`EgressGuard` — the connection-time browser egress guard (plan task A1, F01).

Drives the guard over a real loopback socket, which is the only way to
test what it actually promises: Chromium is launched with
``--proxy-server=http://127.0.0.1:<port>``, so *every* connection the
browser makes — redirect hop, sub-resource, worker, popup, WebSocket —
arrives here as a `CONNECT`/absolute-URI request line. A test that called
an internal helper instead would prove nothing about that wire contract.

**Async driver**: `asyncio.run` around each coroutine body, matching
`tests/unit/test_browser_ssrf.py` and the rest of this suite —
`pytest-asyncio` is not a dependency of this repo (`pyproject.toml`
`[dependency-groups] dev` pins `pytest` alone), so `@pytest.mark.asyncio`
would silently not run. The bodies are otherwise exactly the plan's three
cases: a public name that resolves privately is refused, an IP literal is
refused even though loopback is deliberately NOT in Chromium's bypass
list, and a genuinely public resolution is dialed on the validated
address.
"""

from __future__ import annotations

import asyncio
import base64
import socket

import pytest

from scrape_core.browser.egress_guard import (
    PROXY_LEG_USERNAME,
    EgressGuard,
    GuardDecision,
    UpstreamProxy,
)


class FakeResolver:
    """Stand-in for the process resolver: `host -> [(ip, port), ...]`.

    Same shape as `getaddrinfo`'s sockaddr list (address AND port), which
    is what lets the third case point a "public" hostname at a local
    fixture server on an arbitrary port.
    """

    def __init__(self, table):
        self.table = table

    async def resolve(self, host, port):
        return self.table[host]


@pytest.fixture
def unused_tcp_port() -> int:
    """A currently-free TCP port on loopback.

    Local re-implementation of the `pytest-asyncio` fixture of the same
    name (that plugin is not installed — see the module docstring), so the
    plan's third test reads exactly as written.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_connect_to_privately_resolving_public_name_is_refused() -> None:
    async def body() -> None:
        guard = EgressGuard(resolver=FakeResolver({"evil.example": [("127.0.0.1", 0)]}))
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert guard.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] == 1
        w.close()
        await guard.stop_async()

    asyncio.run(body())


def test_ip_literal_loopback_is_refused_even_with_bypass() -> None:
    async def body() -> None:
        guard = EgressGuard(resolver=FakeResolver({}))
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        w.close()
        await guard.stop_async()

    asyncio.run(body())


def test_public_resolution_is_dialed_on_the_validated_ip(unused_tcp_port: int) -> None:
    async def body() -> None:
        async def ok(r, w):
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()

        srv = await asyncio.start_server(ok, "127.0.0.1", unused_tcp_port)
        guard = EgressGuard(
            resolver=FakeResolver({"shop.example": [("127.0.0.1", unused_tcp_port)]}),
            _allow_loopback_for_tests=True,
        )
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET http://shop.example/ HTTP/1.1\r\nHost: shop.example\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 200")
        w.close()
        srv.close()
        await guard.stop_async()

    asyncio.run(body())


# --------------------------------------------------------------------------
# Guard properties the three plan cases imply but do not pin down on their
# own — each one is a way the guard could "pass" the cases above and still
# not close F01.
# --------------------------------------------------------------------------


def test_all_resolved_addresses_must_be_public_not_just_the_first() -> None:
    """A multi-A-record rebinding answer whose *second* address is private.

    Dialing `addrs[0]` after checking only `addrs[0]` is the classic
    partial fix: the browser's own resolver may pick any of them, and the
    guard's whole value is that it, not Chromium, decides.
    """

    async def body() -> None:
        guard = EgressGuard(
            resolver=FakeResolver({"mixed.example": [("93.184.216.34", 443), ("10.0.0.5", 443)]})
        )
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT mixed.example:443 HTTP/1.1\r\nHost: mixed.example:443\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert guard.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] == 1
        w.close()
        await guard.stop_async()

    asyncio.run(body())


def test_internal_hostname_is_refused_without_resolving() -> None:
    """`metadata.google.internal` and friends never reach the resolver.

    The resolver table is deliberately empty: a guard that resolved first
    would raise `KeyError` and (fail-closed) still refuse, so the assertion
    that matters is the *decision* — the URL-safety layer caught it.
    """

    async def body() -> None:
        guard = EgressGuard(resolver=FakeResolver({}))
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT metadata.google.internal:80 HTTP/1.1\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert guard.decisions[GuardDecision.REJECTED_UNSAFE_URL] == 1
        w.close()
        await guard.stop_async()

    asyncio.run(body())


def test_unresolvable_host_fails_closed() -> None:
    """A resolver error is never "probably fine" — it is a refusal."""

    async def body() -> None:
        guard = EgressGuard(resolver=FakeResolver({}))
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT shop.example:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert guard.decisions[GuardDecision.REJECTED_UNRESOLVABLE] == 1
        w.close()
        await guard.stop_async()

    asyncio.run(body())


def test_connect_tunnel_relays_both_directions(unused_tcp_port: int) -> None:
    """The allowed CONNECT path is a real bidirectional tunnel.

    Without this, "allowed" could mean "200 Connection Established and
    then nothing", which every navigation would time out on.
    """

    async def body() -> None:
        async def echo(r, w):
            w.write(await r.read(5))
            await w.drain()
            w.close()

        srv = await asyncio.start_server(echo, "127.0.0.1", unused_tcp_port)
        guard = EgressGuard(
            resolver=FakeResolver({"shop.example": [("127.0.0.1", unused_tcp_port)]}),
            _allow_loopback_for_tests=True,
        )
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT shop.example:443 HTTP/1.1\r\nHost: shop.example:443\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 200")
        while await r.readline() not in (b"\r\n", b"\n", b""):
            pass
        w.write(b"hello")
        await w.drain()
        assert await r.readexactly(5) == b"hello"
        assert guard.decisions[GuardDecision.ALLOWED_DIRECT] == 1
        w.close()
        srv.close()
        await guard.stop_async()

    asyncio.run(body())


def test_query_strings_are_never_logged(caplog, unused_tcp_port: int) -> None:
    """A refused URL's query string can carry the very secret an SSRF is after."""

    async def body() -> None:
        guard = EgressGuard(resolver=FakeResolver({}))
        port = await guard.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(
            b"GET http://127.0.0.1:9/x?token=SUPERSECRET HTTP/1.1\r\n"
            b"Host: 127.0.0.1:9\r\n\r\n"
        )
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        w.close()
        await guard.stop_async()

    with caplog.at_level("DEBUG"):
        asyncio.run(body())

    assert "SUPERSECRET" not in caplog.text
    assert "token=" not in caplog.text


def test_daemon_thread_wrappers_start_and_stop() -> None:
    """`start()`/`stop()` must work from a thread with no running loop.

    Scrapy's settings module (the real caller) is imported long before any
    reactor exists, and the guard must never install itself into — or
    require — the reactor's loop.
    """
    guard = EgressGuard(resolver=FakeResolver({}))
    port = guard.start()
    try:
        assert isinstance(port, int) and port > 0
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
            assert sock.recv(12).startswith(b"HTTP/1.1 403")
    finally:
        guard.stop()


# --------------------------------------------------------------------------
# Proxied legs (phase-A review, Finding 1).
#
# The first implementation selected the upstream leg from a
# `Proxy-Authorization: Basic leg=proxy:<token>` header the browser was
# expected to present. Chromium never sends one preemptively — Playwright
# supplies per-context proxy credentials only in answer to a `407`, which
# this guard cannot issue because the same listener also serves unproxied
# contexts — so every proxied browser context silently egressed DIRECT from
# the fleet IP while the spider and the cost ledger both recorded PROXY.
#
# The leg is now the listener: `register_upstream()` binds a loopback port
# dedicated to that upstream, and what accepted the connection decides the
# leg. These tests drive a real fake upstream proxy over real sockets, so
# they pin the wire contract (`CONNECT` line + `Proxy-Authorization`) and
# not just a counter.
# --------------------------------------------------------------------------


class FakeUpstreamProxy:
    """Loopback stand-in for the residential provider.

    Records the head of every connection it accepts (that is where the
    forwarded `CONNECT` line and the provider credential show up), answers
    `200 Connection Established`, then echoes the tunnel body back so a
    test can prove the bytes really travelled through it.
    """

    def __init__(self) -> None:
        self.heads: list[list[str]] = []
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader, writer) -> None:
        head: list[str] = []
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            head.append(line.decode("latin-1").rstrip("\r\n"))
        self.heads.append(head)
        if head and head[0].startswith("CONNECT"):
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        writer.close()


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def test_upstream_leg_listener_forwards_connect_with_the_provider_credential() -> None:
    """The whole proxied-leg contract, end to end over sockets.

    Asserts all four halves the review demanded: the connection is taken
    (`ALLOWED_UPSTREAM`), it is NOT dialed direct (`ALLOWED_DIRECT == 0`),
    the upstream received `CONNECT <host>:<port>` for the *name* with the
    provider's own `Proxy-Authorization`, and the tunnel actually carries
    bytes afterwards.
    """

    async def body() -> None:
        upstream_proxy = FakeUpstreamProxy()
        await upstream_proxy.start()

        guard = EgressGuard(
            resolver=FakeResolver({"shop.example": [("93.184.216.34", 443)]})
        )
        direct_port = await guard.start_async()
        leg_port = await guard.register_upstream_async(
            UpstreamProxy(
                server=f"http://127.0.0.1:{upstream_proxy.port}",
                username="di-user;sessid.abc123",
                password="di-secret",
            )
        )
        assert leg_port != direct_port, "the proxied leg must be its own listener"

        r, w = await asyncio.open_connection("127.0.0.1", leg_port)
        w.write(b"CONNECT shop.example:443 HTTP/1.1\r\nHost: shop.example:443\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 200")
        while await r.readline() not in (b"\r\n", b"\n", b""):
            pass
        w.write(b"hello")
        await w.drain()
        assert await r.readexactly(5) == b"hello"

        assert guard.decisions[GuardDecision.ALLOWED_UPSTREAM] == 1
        assert guard.decisions[GuardDecision.ALLOWED_DIRECT] == 0

        (head,) = upstream_proxy.heads
        assert head[0] == "CONNECT shop.example:443 HTTP/1.1"
        assert (
            "Proxy-Authorization: " + _basic("di-user;sessid.abc123", "di-secret")
        ) in head

        w.close()
        await guard.stop_async()
        await upstream_proxy.stop()

    asyncio.run(body())


def test_the_browser_never_receives_the_provider_credential() -> None:
    """A registered leg is addressed by port alone.

    `register_upstream` returns something a Playwright `proxy` kwarg can
    use with NO username/password — which is what keeps the DataImpulse
    credential inside this process.
    """

    async def body() -> None:
        upstream_proxy = FakeUpstreamProxy()
        await upstream_proxy.start()
        guard = EgressGuard(resolver=FakeResolver({}))
        await guard.start_async()
        upstream = UpstreamProxy(
            server=f"http://127.0.0.1:{upstream_proxy.port}",
            username="di-user",
            password="di-secret",
        )
        leg_port = await guard.register_upstream_async(upstream)

        assert isinstance(leg_port, int) and leg_port > 0
        # Registering the same upstream again reuses the listener rather
        # than leaking one per request.
        assert await guard.register_upstream_async(upstream) == leg_port
        assert guard.upstream_port(upstream) == leg_port

        await guard.stop_async()
        await upstream_proxy.stop()

    asyncio.run(body())


def test_upstream_leg_still_validates_the_destination_before_forwarding() -> None:
    """A privately-resolving name is refused on the proxied leg too.

    The residential exit would happily be told to fetch anything; the
    SSRF decision has to happen on this side, before the `CONNECT` is
    forwarded at all.
    """

    async def body() -> None:
        upstream_proxy = FakeUpstreamProxy()
        await upstream_proxy.start()
        guard = EgressGuard(
            resolver=FakeResolver({"evil.example": [("10.0.0.5", 443)]})
        )
        await guard.start_async()
        leg_port = await guard.register_upstream_async(
            UpstreamProxy(server=f"http://127.0.0.1:{upstream_proxy.port}")
        )

        r, w = await asyncio.open_connection("127.0.0.1", leg_port)
        w.write(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert guard.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] == 1
        assert upstream_proxy.heads == [], "the upstream must never have been dialed"

        w.close()
        await guard.stop_async()
        await upstream_proxy.stop()

    asyncio.run(body())


def test_a_proxied_leg_asked_for_the_old_way_fails_closed(unused_tcp_port: int) -> None:
    """A caller still presenting `leg=proxy` credentials is refused.

    Not dialed direct: that silent downgrade — a leg the ledger books as
    PROXY leaving from the fleet's own IP — is exactly the regression this
    rework exists to remove, so the stale wiring must fail loudly.
    """

    async def body() -> None:
        dialed: list[str] = []

        async def origin(r, w):
            dialed.append("dialed")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()

        srv = await asyncio.start_server(origin, "127.0.0.1", unused_tcp_port)
        guard = EgressGuard(
            resolver=FakeResolver({"shop.example": [("127.0.0.1", unused_tcp_port)]}),
            _allow_loopback_for_tests=True,
        )
        port = await guard.start_async()

        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(
            b"CONNECT shop.example:443 HTTP/1.1\r\n"
            b"Host: shop.example:443\r\n"
            + f"Proxy-Authorization: {_basic(PROXY_LEG_USERNAME, 'stale-token')}\r\n".encode()
            + b"\r\n"
        )
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 502")
        assert guard.decisions[GuardDecision.REJECTED_UPSTREAM_REFUSED] == 1
        assert guard.decisions[GuardDecision.ALLOWED_DIRECT] == 0
        assert dialed == []

        w.close()
        srv.close()
        await guard.stop_async()

    asyncio.run(body())


def test_register_upstream_from_another_thread_binds_a_leg() -> None:
    """The spider calls `register_upstream()` from Scrapy's reactor thread.

    The guard's listener lives on its own loop in a daemon thread, so the
    sync wrapper is the production path — and it must fail loudly, never
    hand back the direct port, when there is no such loop.
    """
    guard = EgressGuard(resolver=FakeResolver({}))
    upstream = UpstreamProxy(server="http://127.0.0.1:1", username="u", password="p")

    with pytest.raises(RuntimeError):
        guard.register_upstream(upstream)

    direct_port = guard.start()
    try:
        leg_port = guard.register_upstream(upstream)
        assert leg_port != direct_port
        assert guard.register_upstream(upstream) == leg_port
        with socket.create_connection(("127.0.0.1", leg_port), timeout=5) as sock:
            sock.sendall(b"CONNECT metadata.google.internal:80 HTTP/1.1\r\n\r\n")
            assert sock.recv(12).startswith(b"HTTP/1.1 403")
    finally:
        guard.stop()
