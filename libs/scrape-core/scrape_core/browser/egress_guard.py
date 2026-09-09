"""`EgressGuard` — connection-time SSRF enforcement for the browser path
(READY F01, plan task A1, Constitution §VI NON-NEGOTIABLE).

Why a proxy and not another route hook
--------------------------------------

``scrape_core.browser.ssrf.abort_unsafe_request`` (wired as
``PLAYWRIGHT_ABORT_REQUEST``) is a *route* hook: Chromium hands it the
request it is about to make, and it re-resolves the hostname on its own.
Two structural gaps remain no matter how carefully that hook is written:

* **It sees only the first request of a redirect chain.** Chromium
  follows a 3xx ``Location`` inside the network stack; scrapy-playwright's
  ``route`` handler is attached per navigation, so hop 2..N of a chain
  never reach the hook at all. A public first hop that 302s to
  ``http://127.0.0.1:6379/`` is therefore fetched.
* **Check-then-connect is inherently rebindable.** Even for the requests
  it does see, the hook resolves the name, decides, and then lets
  *Chromium* resolve it again for the actual socket. A DNS answer with a
  1-second TTL changes between those two lookups; the address validated
  is not the address dialed.

This module closes both by moving the decision to the only place that
cannot be bypassed: the connection itself. Chromium is launched with
``--proxy-server=http://127.0.0.1:<port>`` and
``--proxy-bypass-list=<-loopback>`` (the ``<-loopback>`` token *removes*
Chromium's built-in "never proxy localhost" rule, so even an IP-literal
loopback destination is sent here rather than dialed directly), which
makes **every** connection the browser opens — each redirect hop, every
sub-resource, service workers, popups, WebSockets and the WebSocket
upgrade's ``CONNECT`` — arrive as a request line on this listener. The
guard then:

1. Runs the repo's own :func:`app_shared.url_safety.validate_competitor_url`
   on the destination (scheme, userinfo, IDNA/trailing-dot folding, the
   loose-``inet_aton`` IPv4 spellings, internal hostnames, and
   ``_reject_ip`` for IP literals) — so an IP literal is refused here,
   without a resolve.
2. Resolves the hostname **itself** and refuses if the answer is empty or
   if *any* returned address fails :func:`app_shared.url_safety._reject_ip`
   (a rebinding answer that mixes one public and one private A record is
   refused, because the browser's own resolver could have picked either).
3. Dials **the exact address it validated**, never re-resolving. That is
   what actually closes rebinding: there is no second lookup to poison.

The route hook stays wired as defense in depth (and remains the place the
sub-resource cost/category policy lives) — this is an additional layer,
not a replacement.

Proxied legs
------------

A per-context proxy in Playwright *replaces* the launch-level
``--proxy-server``, which would take the whole proxied leg back outside
the guard. So a proxied context points at the guard too — but at a
**dedicated loopback listener of its own**
(:meth:`EgressGuard.register_upstream` returns that listener's port). The
leg is therefore decided by *which socket accepted the connection*, and
nothing else: connections arriving on the main listener are direct legs,
connections arriving on an upstream's listener are forwarded to that
upstream after the identical destination validation.

Selecting the leg from a browser-supplied ``Proxy-Authorization`` header
does **not** work and was the F01 fix's own first bug: Chromium never
sends proxy credentials preemptively. Playwright supplies the per-context
``username``/``password`` only in answer to a ``407 Proxy Authentication
Required`` challenge, and this guard cannot issue an unconditional 407
because the same port also serves unproxied contexts that have no
credentials at all. A credential-driven guard therefore reads "no header"
as "direct leg" and silently egresses the fleet's own IP on a leg the
spider and the cost ledger both record as PROXY. One listener per
upstream removes the ambiguity instead of papering over it: there is no
"undetermined" state to guess at. A request that nonetheless arrives with
``username="leg=proxy"`` credentials on a listener that has **no**
upstream is a stale caller from the credential-based wiring, and is
refused (``REJECTED_UPSTREAM_REFUSED``) rather than dialed direct.

Two consequences worth stating:

* the residential provider's username/password never enter the browser
  process at all — Chromium is handed a bare ``http://127.0.0.1:<port>``
  with no credential of any kind;
* the sticky-session username the spider builds
  (``scrape_core.targets.sticky_proxy_username``) is carried in the
  registered :class:`UpstreamProxy`, so sticky behaviour is unchanged.

The exit node resolves the hostname for itself on the far side, which is
why the forwarded request keeps the *name* rather than the validated IP —
the validation that matters for SSRF (is this destination ours or
internal?) has already happened here, and a residential exit cannot reach
our private network anyway.

Logging
-------

Decisions are counted in :attr:`EgressGuard.decisions` (a
``Counter[GuardDecision]``) and logged with host and port only. **The URL
query string is never logged** — an SSRF probe's query string routinely
carries the credential the probe is trying to exfiltrate, and a refusal
log is exactly where it would end up.

Threading
---------

The listener runs on its own event loop in a daemon thread
(:meth:`start`/:meth:`stop`), so Scrapy's ``AsyncioSelectorReactor`` loop
is untouched and the guard can be started from a settings module at
import time, long before any reactor exists. :meth:`start_async` /
:meth:`stop_async` are the in-loop equivalents used by tests and by any
caller that already has a running loop.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
import threading
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from app_shared.url_safety import (
    UnsafeUrlError,
    UnsafeUrlReason,
    _normalize_host,
    _reject_ip,
    validate_competitor_url,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "EgressGuard",
    "GuardDecision",
    "PROXY_LEG_USERNAME",
    "SystemResolver",
    "UpstreamProxy",
    "ensure_process_guard",
    "get_process_guard",
]

#: The username the *previous* (credential-based) wiring used to ask the
#: guard for an upstream leg. Legs are now chosen by listener port, so
#: nothing sets this any more — it is kept solely so that a caller still
#: wired the old way FAILS CLOSED: a request presenting these credentials
#: on a listener that has no upstream is refused, never dialed direct.
#: See the module docstring's "Proxied legs" section.
PROXY_LEG_USERNAME = "leg=proxy"

#: Fallback for ``BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS`` when the
#: settings object cannot be built (a unit-test process with no
#: environment, say). Mirrors the setting's own default — the setting, not
#: this constant, is what production reads.
_FALLBACK_CONNECT_TIMEOUT_SECONDS = 10.0

#: Bound on the request head the guard is willing to buffer before
#: deciding. A proxy client that never sends a blank line is a resource
#: leak, not a request.
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_LINES = 200

#: Relay chunk size for the bidirectional pumps.
_RELAY_CHUNK_BYTES = 64 * 1024


class GuardDecision(Enum):
    """Every terminal outcome for one client connection, counted in
    :attr:`EgressGuard.decisions`.

    Split finely on purpose: "the guard refused something" is not an
    actionable signal, but "the guard refused a *privately resolving*
    public name" is the F01 event an operator (and the image test) needs
    to be able to assert on.
    """

    ALLOWED_DIRECT = "ALLOWED_DIRECT"
    ALLOWED_UPSTREAM = "ALLOWED_UPSTREAM"
    REJECTED_BAD_REQUEST = "REJECTED_BAD_REQUEST"
    REJECTED_UNSAFE_URL = "REJECTED_UNSAFE_URL"
    REJECTED_PRIVATE_IP_LITERAL = "REJECTED_PRIVATE_IP_LITERAL"
    REJECTED_PRIVATE_RESOLUTION = "REJECTED_PRIVATE_RESOLUTION"
    REJECTED_UNRESOLVABLE = "REJECTED_UNRESOLVABLE"
    REJECTED_CONNECT_FAILED = "REJECTED_CONNECT_FAILED"
    REJECTED_UPSTREAM_REFUSED = "REJECTED_UPSTREAM_REFUSED"


@dataclass(frozen=True)
class UpstreamProxy:
    """A residential/datacenter proxy leg the guard may forward to.

    `username` is the FULL username the provider expects, sticky suffix
    included (``scrape_core.targets.sticky_proxy_username`` has already
    been applied by the caller) — the guard never rebuilds it, so
    "proxied" can never come to mean one thing here and another in the
    cost ledger.
    """

    server: str
    username: str | None = None
    password: str | None = None


class Resolver(Protocol):
    """The one thing the guard needs from DNS.

    Returns sockaddr-shaped ``(address, port)`` pairs, exactly like
    ``loop.getaddrinfo``'s ``sockaddr`` field — the port travels with the
    address so a caller (or a test) can point a name at a specific
    endpoint without a second, unvalidated lookup.
    """

    async def resolve(self, host: str, port: int) -> "Sequence[tuple[str, int]]":
        ...  # pragma: no cover - protocol


class SystemResolver:
    """The production resolver: the event loop's own ``getaddrinfo``.

    Async rather than :class:`scrape_core.safety.resolver.SafeResolver`
    (which is Twisted-shaped: a ``Deferred[str]`` for one A record) —
    this listener runs on a plain asyncio loop in its own thread, and
    needs *every* address for the "reject if ANY is private" rule the
    single-address Twisted resolver cannot express. The safety decision
    itself is not duplicated: both call the same
    :func:`app_shared.url_safety._reject_ip`.
    """

    async def resolve(self, host: str, port: int) -> list[tuple[str, int]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [(info[4][0], int(info[4][1])) for info in infos]


class _GuardRejection(Exception):
    """Internal control flow: refuse this connection with `decision`."""

    def __init__(self, decision: GuardDecision, status: str = "403 Forbidden") -> None:
        self.decision = decision
        self.status = status
        super().__init__(decision.value)


class EgressGuard:
    """A loopback HTTP/CONNECT proxy that only ever dials validated public IPs.

    Args:
        bind_host: interface to listen on. Loopback only in production —
            the guard performs no authentication beyond the upstream-leg
            token, so it must never be reachable off-host.
        upstream: the leg for connections arriving on the guard's MAIN
            listener. ``None`` (the default, and what the process-wide
            guard uses) makes that listener a direct leg; proxied legs get
            their own listener from :meth:`register_upstream`. Set it only
            for a guard whose every connection should be forwarded.
        resolver: DNS seam (:class:`SystemResolver` in production).
        connect_timeout: overrides
            ``BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS``.
        _allow_loopback_for_tests: skip the resolved-address
            ``_reject_ip`` rule entirely so a test can point a "public"
            name at a local fixture server. Never set in production; the
            URL-safety layer (step 1) still runs, so an IP literal is
            refused even with this on.
        _allow_addresses_for_tests: the same exemption, but only for the
            listed addresses. This is what the in-image integration test
            uses: its fixture pages must load from ``127.0.0.1`` while a
            sub-resource resolving to ``10.0.0.5`` must still be refused,
            which the all-or-nothing flag above cannot express.
    """

    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        upstream: UpstreamProxy | None = None,
        resolver: Resolver | None = None,
        *,
        connect_timeout: float | None = None,
        _allow_loopback_for_tests: bool = False,
        _allow_addresses_for_tests: "Sequence[str]" = (),
    ) -> None:
        self.bind_host = bind_host
        self.upstream = upstream
        self.resolver: Resolver = resolver if resolver is not None else SystemResolver()
        self.decisions: Counter[GuardDecision] = Counter()

        self._allow_loopback_for_tests = _allow_loopback_for_tests
        self._allow_addresses_for_tests = frozenset(_allow_addresses_for_tests)
        self._connect_timeout = connect_timeout
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        # One dedicated listener per registered upstream: the accepting
        # socket, not a header, is what selects the leg.
        self._upstream_ports: dict[UpstreamProxy, int] = {}
        self._upstream_servers: dict[UpstreamProxy, asyncio.AbstractServer] = {}
        self._upstreams_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        """The bound port. Raises if the guard is not running."""
        if self._port is None:
            raise RuntimeError("EgressGuard is not running")
        return self._port

    async def start_async(self) -> int:
        """Bind and serve on the *current* event loop; returns the port."""
        if self._server is not None:
            return self.port
        self._server = await asyncio.start_server(
            self._client_callback(self.upstream), self.bind_host, 0, backlog=128
        )
        self._port = int(self._server.sockets[0].getsockname()[1])
        logger.info(
            "egress_guard: listening on %s:%s (upstream=%s)",
            self.bind_host,
            self._port,
            "configured" if self.upstream is not None else "none",
        )
        return self._port

    async def stop_async(self) -> None:
        """Stop accepting and wait for the listener to close.

        In-flight tunnels are not force-killed: each pump ends when its
        socket does, and the process-wide guard outlives every spider run
        anyway. This exists so a test leaves no listening socket behind.
        """
        with self._upstreams_lock:
            upstream_servers = list(self._upstream_servers.values())
            self._upstream_servers.clear()
            self._upstream_ports.clear()
        server, self._server = self._server, None
        self._port = None
        if server is not None:
            upstream_servers.append(server)
        for listener in upstream_servers:
            listener.close()
            try:
                await listener.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown must never raise into a caller
                logger.debug("egress_guard: error while closing listener", exc_info=True)

    def start(self) -> int:
        """Start on a private event loop in a daemon thread; returns the port.

        The daemon thread is what lets a Scrapy *settings module* start
        the guard at import time: there is no reactor yet, and once there
        is one it must not be shared with a long-lived relay.
        """
        if self._thread is not None:
            return self.port

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run, name="egress-guard", daemon=True)
        thread.start()
        ready.wait(timeout=10)

        future = asyncio.run_coroutine_threadsafe(self.start_async(), loop)
        try:
            port = future.result(timeout=10)
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()
            raise

        self._thread = thread
        self._thread_loop = loop
        return port

    def stop(self) -> None:
        """Stop a :meth:`start`-ed guard and join its thread."""
        thread, self._thread = self._thread, None
        loop, self._thread_loop = self._thread_loop, None
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.stop_async(), loop).result(timeout=10)
        except Exception:  # noqa: BLE001 - shutdown must never raise into a caller
            logger.debug("egress_guard: error stopping listener", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)
        loop.close()

    # -- upstream registration --------------------------------------------

    async def register_upstream_async(self, upstream: UpstreamProxy) -> int:
        """Bind a loopback listener dedicated to `upstream`; return its port.

        The port IS the leg: everything accepted on it is forwarded to
        `upstream` (after the same destination validation as a direct
        dial), and nothing else can select that leg. The browser is then
        pointed at ``http://127.0.0.1:<port>`` with no credential at all,
        so the provider's username/password never cross the browser
        boundary — and, unlike a credential, a port cannot fail to be
        presented (see the module docstring's "Proxied legs").

        Registering the same upstream twice returns the same port, so a
        long crawl on one provider does not accumulate listeners.

        Must run on the guard's own event loop; :meth:`register_upstream`
        is the wrapper for callers on another thread (the spider).
        """
        with self._upstreams_lock:
            existing = self._upstream_ports.get(upstream)
        if existing is not None:
            return existing

        server = await asyncio.start_server(
            self._client_callback(upstream), self.bind_host, 0, backlog=128
        )
        port = int(server.sockets[0].getsockname()[1])
        with self._upstreams_lock:
            raced = self._upstream_ports.get(upstream)
            if raced is not None:
                server.close()
                return raced
            self._upstream_ports[upstream] = port
            self._upstream_servers[upstream] = server
        logger.info(
            "egress_guard: upstream leg listening on %s:%s", self.bind_host, port
        )
        return port

    def register_upstream(self, upstream: UpstreamProxy) -> int:
        """Thread-safe :meth:`register_upstream_async` for a :meth:`start`-ed guard.

        This is what the spider calls, from Scrapy's reactor thread, while
        the guard's listener loop runs in its own daemon thread. It fails
        loudly rather than returning the direct-leg port: a proxied context
        that quietly became a direct one is the exact regression F01's
        review caught, so "cannot determine the leg" must never resolve to
        "dial direct".
        """
        with self._upstreams_lock:
            existing = self._upstream_ports.get(upstream)
        if existing is not None:
            return existing

        loop = self._thread_loop
        if loop is None:
            raise RuntimeError(
                "EgressGuard.register_upstream() needs a start()-ed guard; "
                "inside the guard's own loop use register_upstream_async()"
            )
        return asyncio.run_coroutine_threadsafe(
            self.register_upstream_async(upstream), loop
        ).result(timeout=10)

    def upstream_port(self, upstream: UpstreamProxy) -> int | None:
        """The listener port already registered for `upstream`, if any."""
        with self._upstreams_lock:
            return self._upstream_ports.get(upstream)

    # -- request handling --------------------------------------------------

    @property
    def connect_timeout(self) -> float:
        if self._connect_timeout is None:
            try:
                from app_shared.config import get_settings

                self._connect_timeout = float(
                    get_settings().BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS
                )
            except Exception:  # noqa: BLE001 - see _FALLBACK_CONNECT_TIMEOUT_SECONDS
                self._connect_timeout = _FALLBACK_CONNECT_TIMEOUT_SECONDS
        return self._connect_timeout

    def _client_callback(self, upstream: UpstreamProxy | None):
        """`asyncio.start_server` callback bound to one listener's leg."""

        async def _accept(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await self._handle_client(reader, writer, upstream)

        return _accept

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy | None = None,
    ) -> None:
        try:
            await self._serve(reader, writer, upstream)
        except _GuardRejection as rejection:
            self.decisions[rejection.decision] += 1
            await self._refuse(writer, rejection)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 - one bad connection never kills the listener
            logger.warning("egress_guard: unhandled error serving a connection", exc_info=True)
        finally:
            _close(writer)

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy | None = None,
    ) -> None:
        request_line = await reader.readline()
        if not request_line:
            return
        method, target, version = _parse_request_line(request_line)
        headers = await _read_headers(reader)

        if method == "CONNECT":
            host, port = _split_authority(target, default_port=443)
            scheme = "https"
        else:
            split = urlsplit(target)
            if split.scheme not in ("http", "https") or not split.hostname:
                # Origin-form: the client is not talking to a proxy at all.
                raise _GuardRejection(GuardDecision.REJECTED_BAD_REQUEST, "400 Bad Request")
            scheme = split.scheme
            host = split.hostname
            port = split.port or (443 if scheme == "https" else 80)

        addrs = await self._validate_destination(scheme, host, port)

        # Fail closed. `upstream` came from the listener that accepted this
        # connection, so it is never ambiguous — but a caller still wired
        # the old credential way (`username="leg=proxy"`) is asking for a
        # leg this listener cannot serve, and the one thing that must not
        # happen is answering that request over the fleet's own egress.
        if upstream is None and _wants_upstream_leg(headers):
            logger.warning(
                "egress_guard: proxied leg requested for %s:%s on the direct "
                "listener; refusing rather than dialing direct",
                host,
                port,
            )
            raise _GuardRejection(
                GuardDecision.REJECTED_UPSTREAM_REFUSED, "502 Bad Gateway"
            )

        if upstream is not None:
            await self._serve_via_upstream(
                reader, writer, upstream, method, target, version, headers, host, port
            )
            return

        ip, dial_port = addrs[0]
        try:
            origin_reader, origin_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, dial_port or port), timeout=self.connect_timeout
            )
        except Exception as exc:  # noqa: BLE001 - a dead destination is not a bug here
            logger.info(
                "egress_guard: dial failed for %s:%s (%s)", host, port, type(exc).__name__
            )
            raise _GuardRejection(
                GuardDecision.REJECTED_CONNECT_FAILED, "502 Bad Gateway"
            ) from exc

        self.decisions[GuardDecision.ALLOWED_DIRECT] += 1
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            origin_writer.write(_origin_form_request(method, target, version, headers))
            await origin_writer.drain()
        await _relay(reader, writer, origin_reader, origin_writer)

    async def _serve_via_upstream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: UpstreamProxy,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        host: str,
        port: int,
    ) -> None:
        """Forward to `upstream` — only ever AFTER `_validate_destination`.

        The hostname (not the validated IP) is forwarded on purpose: the
        residential exit resolves for itself on the far side, and it has
        no route to our private network. The SSRF question — "is this
        destination internal to us?" — was already answered here.
        """
        up_host, up_port = _split_authority(
            urlsplit(upstream.server).netloc or upstream.server, default_port=80
        )
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(up_host, up_port), timeout=self.connect_timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "egress_guard: upstream proxy dial failed (%s)", type(exc).__name__
            )
            raise _GuardRejection(
                GuardDecision.REJECTED_UPSTREAM_REFUSED, "502 Bad Gateway"
            ) from exc

        head = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if upstream.username is not None:
            credential = f"{upstream.username}:{upstream.password or ''}".encode("utf-8")
            head.append(
                "Proxy-Authorization: Basic "
                + base64.b64encode(credential).decode("ascii")
            )
        up_writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()

        status_line = await asyncio.wait_for(
            up_reader.readline(), timeout=self.connect_timeout
        )
        await _read_headers(up_reader)
        if not status_line.startswith(b"HTTP/1.") or status_line.split(b" ")[1:2] != [b"200"]:
            _close(up_writer)
            logger.warning(
                "egress_guard: upstream refused CONNECT to %s:%s (%s)",
                host,
                port,
                status_line.decode("latin-1", "replace").strip(),
            )
            raise _GuardRejection(
                GuardDecision.REJECTED_UPSTREAM_REFUSED, "502 Bad Gateway"
            )

        self.decisions[GuardDecision.ALLOWED_UPSTREAM] += 1
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(_origin_form_request(method, target, version, headers))
            await up_writer.drain()
        await _relay(reader, writer, up_reader, up_writer)

    async def _validate_destination(
        self, scheme: str, host: str, port: int
    ) -> list[tuple[str, int]]:
        """The whole safety decision, in the order the plan specifies.

        Returns the validated sockaddr list — the caller dials
        ``result[0]`` and nothing else, so there is no second lookup to
        rebind.
        """
        normalized = _normalize_host(host)
        if not normalized:
            raise _GuardRejection(GuardDecision.REJECTED_UNSAFE_URL)

        # Step 1 — the repo's own URL safety rules (scheme/userinfo/IDNA/
        # loose-IPv4/internal-hostname/IP-literal `_reject_ip`). Reused,
        # never re-implemented: a divergence here would be a bypass.
        try:
            validate_competitor_url(urlunsplit((scheme, f"{normalized}:{port}", "/", "", "")))
        except UnsafeUrlError as exc:
            decision = (
                GuardDecision.REJECTED_PRIVATE_IP_LITERAL
                if exc.reason is UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP
                else GuardDecision.REJECTED_UNSAFE_URL
            )
            logger.warning(
                "egress_guard: refusing %s:%s (%s)", normalized, port, exc.reason.value
            )
            raise _GuardRejection(decision) from exc

        # An IP literal that survived step 1 is public and needs no lookup;
        # dialing it *is* dialing the validated address.
        literal = _as_ip_literal(normalized)
        if literal is not None:
            return [(str(literal), port)]

        # Step 2 — resolve here, once, and refuse unless EVERY answer is
        # public (a rebinding answer mixing public and private records is
        # refused: the browser's resolver could have picked either).
        try:
            resolved = await asyncio.wait_for(
                self.resolver.resolve(normalized, port), timeout=self.connect_timeout
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never "probably fine"
            logger.info(
                "egress_guard: resolution failed for %s:%s (%s)",
                normalized,
                port,
                type(exc).__name__,
            )
            raise _GuardRejection(GuardDecision.REJECTED_UNRESOLVABLE) from exc

        addrs = [(str(a), int(p)) for a, p in resolved]
        if not addrs:
            raise _GuardRejection(GuardDecision.REJECTED_UNRESOLVABLE)

        if not self._allow_loopback_for_tests:
            for address, _ in addrs:
                if address in self._allow_addresses_for_tests:
                    continue
                try:
                    ip = ipaddress.ip_address(address)
                except ValueError as exc:
                    raise _GuardRejection(GuardDecision.REJECTED_PRIVATE_RESOLUTION) from exc
                if _reject_ip(ip):
                    logger.warning(
                        "egress_guard: %s:%s resolved to a non-public address; refusing",
                        normalized,
                        port,
                    )
                    raise _GuardRejection(GuardDecision.REJECTED_PRIVATE_RESOLUTION)

        # Step 3 — the caller dials addrs[0] verbatim. No re-resolution.
        return addrs

    async def _refuse(self, writer: asyncio.StreamWriter, rejection: _GuardRejection) -> None:
        body = f"egress guard: {rejection.decision.value}\n".encode("ascii")
        try:
            writer.write(
                f"HTTP/1.1 {rejection.status}\r\n".encode("latin-1")
                + b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001 - the client may already be gone
            logger.debug("egress_guard: could not write refusal", exc_info=True)


# --------------------------------------------------------------------------
# Wire helpers — deliberately module-level so they are trivially testable
# and carry no connection state.
# --------------------------------------------------------------------------


def _parse_request_line(raw: bytes) -> tuple[str, str, str]:
    try:
        line = raw.decode("latin-1").rstrip("\r\n")
    except Exception as exc:  # noqa: BLE001
        raise _GuardRejection(GuardDecision.REJECTED_BAD_REQUEST, "400 Bad Request") from exc
    parts = line.split(" ")
    if len(parts) != 3:
        raise _GuardRejection(GuardDecision.REJECTED_BAD_REQUEST, "400 Bad Request")
    return parts[0].upper(), parts[1], parts[2]


async def _read_headers(reader: asyncio.StreamReader) -> list[tuple[str, str]]:
    """Read to the blank line, bounded. Order is preserved for forwarding."""
    headers: list[tuple[str, str]] = []
    total = 0
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            return headers
        total += len(line)
        if total > _MAX_HEADER_BYTES or len(headers) >= _MAX_HEADER_LINES:
            raise _GuardRejection(GuardDecision.REJECTED_BAD_REQUEST, "431 Request Header Fields Too Large")
        decoded = line.decode("latin-1").rstrip("\r\n")
        name, _, value = decoded.partition(":")
        headers.append((name.strip(), value.strip()))


def _split_authority(authority: str, *, default_port: int) -> tuple[str, int]:
    """``host:port`` / ``[v6]:port`` / bare host -> ``(host, port)``."""
    authority = authority.strip()
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else default_port
    host, _, port = authority.rpartition(":")
    if not host or not port.isdigit():
        return authority, default_port
    return host, int(port)


def _as_ip_literal(host: str) -> Any:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers:
        if key.lower() == lowered:
            return value
    return None


def _proxy_credentials(headers: list[tuple[str, str]]) -> tuple[str, str] | None:
    raw = _header(headers, "Proxy-Authorization")
    if not raw:
        return None
    scheme, _, encoded = raw.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a malformed credential is simply "no credential"
        return None
    username, _, password = decoded.partition(":")
    return username, password


def _wants_upstream_leg(headers: list[tuple[str, str]]) -> bool:
    """Did this client ask for a proxied leg the old, credential-based way?

    Nothing in this repo does any more (the leg is the listener port), so
    a `True` here means a stale caller — and the only safe answer to it is
    a refusal, never a direct dial. See `_serve`.
    """
    credentials = _proxy_credentials(headers)
    return credentials is not None and credentials[0].startswith(PROXY_LEG_USERNAME)


def _origin_form_request(
    method: str, target: str, version: str, headers: list[tuple[str, str]]
) -> bytes:
    """Rewrite an absolute-URI proxy request to the origin form a server expects.

    `Proxy-*` headers are hop-by-hop and are dropped — forwarding
    ``Proxy-Authorization`` would hand the guard's own leg token to the
    destination site.
    """
    split = urlsplit(target)
    path = urlunsplit(("", "", split.path or "/", split.query, ""))
    lines = [f"{method} {path} {version}"]
    lines += [
        f"{name}: {value}"
        for name, value in headers
        if not name.lower().startswith("proxy-")
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(_RELAY_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 - a relay error ends the relay, nothing more
        logger.debug("egress_guard: relay error", exc_info=True)
    finally:
        _close(writer)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    await asyncio.gather(
        _pump(client_reader, remote_writer),
        _pump(remote_reader, client_writer),
        return_exceptions=True,
    )


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
    except Exception:  # noqa: BLE001 - close is best effort
        pass


# --------------------------------------------------------------------------
# Process-wide guard — one listener per Scrapyd spider process.
# --------------------------------------------------------------------------

_process_guard: EgressGuard | None = None
_process_guard_lock = threading.Lock()


def ensure_process_guard() -> EgressGuard:
    """Start (once) and return this process's guard.

    Called from the browser project's settings module at import time, so
    ``PLAYWRIGHT_LAUNCH_OPTIONS`` can name the port. Idempotent: a Scrapy
    settings module can be imported more than once in one process (tests
    do exactly that) and must not leak a second listener each time.

    Deliberately **not** exception-swallowing. If the guard cannot bind,
    the browser node would run with route-hook-only coverage — the exact
    state F01 exists to end — so the import fails loudly instead.
    """
    global _process_guard
    with _process_guard_lock:
        if _process_guard is None:
            guard = EgressGuard()
            guard.start()
            _process_guard = guard
        return _process_guard


def get_process_guard() -> EgressGuard | None:
    """This process's guard if one is already running, else ``None``.

    Never starts one. The spider uses this rather than
    :func:`ensure_process_guard` because the question it actually needs
    answered is "was Chromium launched behind the guard?" — and only the
    settings module, which owns `PLAYWRIGHT_LAUNCH_OPTIONS`, can make that
    true. A unit test that exercises the spider without importing that
    settings module therefore gets `None` and the pre-F01 direct proxy
    kwargs, instead of silently spawning a listener thread per test.
    """
    with _process_guard_lock:
        return _process_guard
