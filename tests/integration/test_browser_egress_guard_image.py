"""In-image proof that the browser cannot egress past the guard (READY F01,
plan task A1, `contracts/browser-safety.md`).

This is the test the `apps/scrapers-browser` Dockerfile runs as a build
stage: **if the guard can be bypassed, the image does not build.** That is
the point of it living here rather than only in the unit suite — the unit
tests exercise the guard's own wire protocol, but only a real Chromium can
demonstrate that Chromium honours `--proxy-server`/`--proxy-bypass-list`
for the four egress paths the route hook cannot see:

1. **redirect-to-loopback** — a public first hop that 302s to
   ``http://127.0.0.1:<port>/secret``. Chromium follows this inside its
   own network stack, so `PLAYWRIGHT_ABORT_REQUEST` never sees hop 2.
2. **privately-resolving sub-resource** — ``<script
   src="http://private.test/x.js">`` where ``private.test`` resolves to
   10.0.0.5. The URL looks public; only a resolve catches it.
3. **service worker** — a worker outlives its page and re-issues fetches
   with no route hook attached. `BROWSER_SERVICE_WORKERS="block"` stops
   registration outright.
4. **popup** — ``window.open`` opens a page in a context the spider never
   configured.
5. **proxied leg** — a per-context ``proxy`` pointed at the listener the
   guard bound for one upstream. Only a real browser can show that
   Chromium puts every hop of a proxied context on that listener, which
   is what the credential-based first implementation got wrong (see the
   case-5 section at the bottom of this module).

Every case asserts BOTH halves: the guard counted a refusal, **and**
``/secret`` was never requested. The second half is the one that matters —
a decision counter can be right while the request still went out.

**No database, no Redis, no Scrapyd.** Unlike the other
``test_browser_*_live.py`` modules, this one needs nothing but Chromium and
a loopback socket, because a Docker build stage has no live stack. It is
marked `integration` (it needs a real browser, so it is not unit-suite
work) and `browser` (so the image stage can select exactly it).

Guard wiring under test: the arguments come from the REAL settings module
(`price_monitor_browser.settings.PLAYWRIGHT_LAUNCH_OPTIONS`), so a
regression that drops `--proxy-bypass-list=<-loopback>` fails here. Only
the port is redirected, to a guard whose resolver is a fixed table — the
production guard's resolver is the system one, and `private.test` /
`fixture.test` do not exist in DNS.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import socket
import threading
from collections.abc import Iterator

import pytest

from scrape_core.browser.egress_guard import (
    EgressGuard,
    GuardDecision,
    UpstreamProxy,
)

pytestmark = [pytest.mark.integration, pytest.mark.browser]

#: The address the private-DNS sub-resource case resolves to. Private, so
#: `_reject_ip` denies it; never dialed, because the guard refuses first.
_PRIVATE_ADDRESS = "10.0.0.5"

#: Container-launch arguments, added to (never replacing) the real settings
#: module's arguments. The Dockerfile build stage runs as root and with the
#: default 64 MB /dev/shm, where Chromium's own sandbox refuses to start —
#: without these the whole module would SKIP and the build gate would pass
#: vacuously, which is worse than not having it. Neither flag touches proxy
#: behaviour, which is what this module actually asserts.
_CONTAINER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _chromium_reachable() -> bool:
    """Best-effort probe that a Chromium binary is actually installed.

    Mirrors `tests/integration/_browser_spider_live_support.chromium_reachable`
    rather than importing it: that module also pulls in the live-stack
    Postgres/Redis probe, and this test deliberately needs neither.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(args=_CONTAINER_ARGS)
            browser.close()
    except Exception:
        return False
    return True


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serves the four egress-attempt pages plus the honeypot `/secret`."""

    #: Every path `/secret` was requested on. Class-level so the test can
    #: read it without threading state through `BaseHTTPRequestHandler`.
    secret_hits: list[str] = []

    def log_message(self, *args: object) -> None:  # noqa: A003 - stdlib hook
        """Silence the default stderr access log."""

    def _html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        port = self.server.server_address[1]
        path = self.path.split("?")[0]

        if path == "/secret":
            # Reaching this line at all is the failure the whole module
            # exists to prevent.
            type(self).secret_hits.append(self.path)
            self._html("<html><body>secret</body></html>")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/sub":
            self._html(
                '<html><body><script src="http://private.test/x.js"></script>'
                "done</body></html>"
            )
        elif path == "/worker":
            self._html(
                "<html><body><script>"
                "window.__sw = navigator.serviceWorker"
                " ? navigator.serviceWorker.register('/sw.js')"
                "     .then(() => 'registered').catch(e => 'failed: ' + e)"
                " : Promise.resolve('unavailable');"
                "</script>worker</body></html>"
            )
        elif path == "/sw.js":
            payload = f"self.addEventListener('install',()=>fetch('http://127.0.0.1:{port}/secret'));".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif path == "/popup":
            self._html(
                "<html><body><script>"
                f"window.open('http://127.0.0.1:{port}/secret');"
                "</script>popup</body></html>"
            )
        else:
            self.send_error(404)


@pytest.fixture
def fixture_server() -> Iterator[int]:
    _FixtureHandler.secret_hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _TableResolver:
    """Fixed DNS for the two names the fixture pages use."""

    def __init__(self, fixture_port: int) -> None:
        self._table = {
            "fixture.test": [("127.0.0.1", fixture_port)],
            "private.test": [(_PRIVATE_ADDRESS, 80)],
        }

    async def resolve(self, host: str, port: int):
        return self._table[host]


@pytest.fixture
def guard(fixture_server: int) -> Iterator[EgressGuard]:
    """A guard whose only exemption is the fixture server's own address.

    NOT `_allow_loopback_for_tests=True`: that would exempt every private
    address, and case 2's whole point is that 10.0.0.5 is still refused
    while the fixture pages load.
    """
    started = EgressGuard(
        resolver=_TableResolver(fixture_server),
        _allow_addresses_for_tests=("127.0.0.1",),
    )
    started.start()
    try:
        yield started
    finally:
        started.stop()


@pytest.fixture
def launch_args(monkeypatch: pytest.MonkeyPatch, guard: EgressGuard) -> list[str]:
    """The REAL settings module's launch args, repointed at the test guard.

    Importing the settings module needs the same environment every Scrapy
    project settings module needs (`app_shared.config.get_settings()` runs
    at import) — supplied here so a Docker build stage with no `.env` can
    still run this.
    """
    import importlib

    for name, value in {
        "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/db",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "SCRAPYD_HTTP_URLS": "http://127.0.0.1:1",
        "SCRAPYD_BROWSER_URLS": "http://127.0.0.1:1",
        "SCRAPYD_USERNAME": "scrapyd",
        "SCRAPYD_PASSWORD": "scrapyd",
        "JWT_SECRET": "test-jwt-secret",
        "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
    }.items():
        monkeypatch.setenv(name, value)

    from app_shared.config import get_settings

    get_settings.cache_clear()
    settings_module = importlib.import_module("price_monitor_browser.settings")
    options = settings_module.PLAYWRIGHT_LAUNCH_OPTIONS

    args = list(options.get("args", []))
    assert any(
        arg == "--proxy-bypass-list=<-loopback>" for arg in args
    ), f"settings dropped the loopback bypass removal: {args}"
    assert any(
        arg.startswith("--proxy-server=http://127.0.0.1:") for arg in args
    ), f"settings did not launch Chromium behind the guard: {args}"

    return [
        f"--proxy-server=http://127.0.0.1:{guard.port}"
        if arg.startswith("--proxy-server=")
        else arg
        for arg in args
    ] + _CONTAINER_ARGS


@pytest.fixture
def page_factory(launch_args: list[str]):
    """Chromium launched exactly as production launches it."""
    from playwright.sync_api import sync_playwright

    from app_shared.config import get_settings

    with sync_playwright() as p:
        browser = p.chromium.launch(args=launch_args)
        context = browser.new_context(
            service_workers=get_settings().BROWSER_SERVICE_WORKERS
        )
        try:
            yield context
        finally:
            context.close()
            browser.close()


@pytest.mark.skipif(not _chromium_reachable(), reason="no Chromium binary installed")
def test_redirect_to_loopback_is_refused_at_the_hop_the_route_hook_never_sees(
    guard: EgressGuard, page_factory, fixture_server: int
) -> None:
    page = page_factory.new_page()
    try:
        page.goto(f"http://fixture.test:{fixture_server}/redirect", wait_until="load")
    except Exception:
        # A refused hop surfaces as a Playwright navigation error; that is
        # the intended outcome, not a test failure.
        pass

    assert guard.decisions[GuardDecision.REJECTED_PRIVATE_IP_LITERAL] >= 1
    assert _FixtureHandler.secret_hits == []


@pytest.mark.skipif(not _chromium_reachable(), reason="no Chromium binary installed")
def test_privately_resolving_subresource_is_refused(
    guard: EgressGuard, page_factory, fixture_server: int
) -> None:
    page = page_factory.new_page()
    page.goto(f"http://fixture.test:{fixture_server}/sub", wait_until="load")
    page.wait_for_timeout(1000)

    assert guard.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] >= 1
    assert _FixtureHandler.secret_hits == []


@pytest.mark.skipif(not _chromium_reachable(), reason="no Chromium binary installed")
def test_service_worker_registration_is_blocked(
    page_factory, fixture_server: int
) -> None:
    page = page_factory.new_page()
    page.goto(f"http://fixture.test:{fixture_server}/worker", wait_until="load")
    outcome = page.evaluate("() => window.__sw")
    page.wait_for_timeout(1000)

    assert outcome != "registered", "a service worker registered despite the block"
    assert _FixtureHandler.secret_hits == []


@pytest.mark.skipif(not _chromium_reachable(), reason="no Chromium binary installed")
def test_popup_cannot_reach_a_loopback_target(
    guard: EgressGuard, page_factory, fixture_server: int
) -> None:
    page = page_factory.new_page()
    page.goto(f"http://fixture.test:{fixture_server}/popup", wait_until="load")
    page.wait_for_timeout(1500)

    assert guard.decisions[GuardDecision.REJECTED_PRIVATE_IP_LITERAL] >= 1
    assert _FixtureHandler.secret_hits == []


def test_guard_refuses_a_private_resolution_without_a_browser() -> None:
    """The one case that runs even with no Chromium installed.

    Keeps this module from silently contributing nothing on a machine
    without browser binaries — a fully skipped file looks identical to a
    passing one in CI output.
    """

    async def body() -> None:
        started = EgressGuard(resolver=_TableResolver(1))
        port = await started.start_async()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT private.test:80 HTTP/1.1\r\n\r\n")
        await w.drain()
        assert (await r.readline()).startswith(b"HTTP/1.1 403")
        assert started.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] == 1
        w.close()
        await started.stop_async()

    asyncio.run(body())


# --------------------------------------------------------------------------
# Case 5: the PROXIED leg (phase-A review, Finding 1).
#
# The first implementation chose the upstream leg from a
# `Proxy-Authorization: Basic leg=proxy:<token>` header the browser was
# supposed to present. Chromium never sends proxy credentials preemptively
# — Playwright answers a `407` challenge, which the guard cannot issue on a
# port that also serves unproxied contexts — so every proxied context
# quietly egressed DIRECT from the fleet IP while the cost ledger booked
# PROXY. The leg is now a dedicated loopback listener per upstream, and
# only a real browser can prove that Chromium's per-context `proxy` kwarg
# actually puts every hop on that listener. Hence this case, in the same
# image stage as the other four.
# --------------------------------------------------------------------------

#: The provider credential the fake upstream must see forwarded, and which
#: must NEVER appear in the browser's own proxy configuration. Fake values
#: local to this test; the real ones live in the database, encrypted.
_UPSTREAM_USERNAME = "fake-provider-user;sessid.test123"
_UPSTREAM_PASSWORD = "fake-provider-password"


class _FakeUpstreamProxy:
    """A loopback stand-in for DataImpulse, on blocking sockets.

    Threaded rather than asyncio because this module drives Playwright's
    *sync* API. Records the head of every connection (that is where the
    forwarded `CONNECT` line and the provider credential appear), then
    tunnels to whatever its own resolution table says the requested name
    is — `fixture.test` exists in no DNS, and the point of the exit node
    is precisely that it resolves for itself on the far side.
    """

    def __init__(self, table: dict[str, tuple[str, int]]) -> None:
        self._table = table
        self.heads: list[list[str]] = []
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self.port = int(self._listener.getsockname()[1])
        self._closed = False
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)
        self._thread.start()

    @property
    def connect_targets(self) -> list[str]:
        with self._lock:
            return [head[0] for head in self.heads if head]

    def saw_credential(self) -> bool:
        expected = "Proxy-Authorization: Basic " + base64.b64encode(
            f"{_UPSTREAM_USERNAME}:{_UPSTREAM_PASSWORD}".encode()
        ).decode("ascii")
        with self._lock:
            return any(expected in head for head in self.heads)

    def close(self) -> None:
        self._closed = True
        self._listener.close()
        self._thread.join(timeout=5)

    def _accept_forever(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                head += chunk
            raw, _, rest = head.partition(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            with self._lock:
                self.heads.append(lines)

            parts = lines[0].split(" ")
            host, _, _port = parts[1].rpartition(":") if len(parts) > 1 else ("", "", "")
            target = self._table.get(host)
            if parts[0] != "CONNECT" or target is None:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return

            origin = socket.create_connection(target, timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                origin.sendall(rest)
            pump = threading.Thread(target=_pipe, args=(origin, conn), daemon=True)
            pump.start()
            _pipe(conn, origin)
            pump.join(timeout=5)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


@pytest.fixture
def upstream_proxy(fixture_server: int) -> Iterator[_FakeUpstreamProxy]:
    proxy = _FakeUpstreamProxy({"fixture.test": ("127.0.0.1", fixture_server)})
    try:
        yield proxy
    finally:
        proxy.close()


@pytest.fixture
def proxied_context(
    launch_args: list[str], guard: EgressGuard, upstream_proxy: _FakeUpstreamProxy
):
    """Chromium, with a per-context proxy wired exactly as the spider wires it.

    `register_upstream()` returns the listener port and the context kwargs
    carry NO username/password — the same two facts
    `generic_browser_price_spider._browser_request_for` relies on.
    """
    from playwright.sync_api import sync_playwright

    from app_shared.config import get_settings

    leg_port = guard.register_upstream(
        UpstreamProxy(
            server=f"http://127.0.0.1:{upstream_proxy.port}",
            username=_UPSTREAM_USERNAME,
            password=_UPSTREAM_PASSWORD,
        )
    )
    assert leg_port != guard.port, "the proxied leg must be its own listener"
    proxy_kwargs = {"server": f"http://127.0.0.1:{leg_port}"}
    assert "username" not in proxy_kwargs and "password" not in proxy_kwargs

    with sync_playwright() as p:
        browser = p.chromium.launch(args=launch_args)
        context = browser.new_context(
            proxy=proxy_kwargs,
            service_workers=get_settings().BROWSER_SERVICE_WORKERS,
        )
        try:
            yield context
        finally:
            context.close()
            browser.close()


@pytest.mark.skipif(not _chromium_reachable(), reason="no Chromium binary installed")
def test_proxied_context_really_takes_the_upstream_leg(
    guard: EgressGuard,
    proxied_context,
    upstream_proxy: _FakeUpstreamProxy,
    fixture_server: int,
) -> None:
    """A real browser proves the leg is taken — and still fails closed.

    Three assertions, one per way the old wiring was wrong:

    * the upstream saw ``CONNECT fixture.test:<port>`` with the provider
      credential (the leg was taken, and the credential stayed on this
      side of the browser boundary);
    * the guard counted `ALLOWED_UPSTREAM` and **zero** `ALLOWED_DIRECT`
      (nothing leaked out of the fleet's own egress on a leg the ledger
      books as PROXY);
    * the redirect hop to ``http://127.0.0.1:<port>/secret`` — which
      Chromium follows inside its own network stack — was refused on the
      proxied listener too, and `/secret` was never served.
    """
    page = proxied_context.new_page()
    try:
        page.goto(f"http://fixture.test:{fixture_server}/redirect", wait_until="load")
    except Exception:
        # The refused hop surfaces as a navigation error; intended.
        pass

    assert any(
        target == f"CONNECT fixture.test:{fixture_server} HTTP/1.1"
        for target in upstream_proxy.connect_targets
    ), f"the proxied leg was not forwarded upstream: {upstream_proxy.connect_targets}"
    assert upstream_proxy.saw_credential(), (
        "the upstream did not receive the provider's Proxy-Authorization"
    )
    assert guard.decisions[GuardDecision.ALLOWED_UPSTREAM] >= 1
    assert guard.decisions[GuardDecision.ALLOWED_DIRECT] == 0
    assert guard.decisions[GuardDecision.REJECTED_PRIVATE_IP_LITERAL] >= 1
    assert _FixtureHandler.secret_hits == []
