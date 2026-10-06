"""robots.txt fetch goes through the SSRF guard (security plan 2026-10-02, E1).

`default_robots_fetcher` must:

- resolve + validate the robots host with the same
  `validate_resolved_target` the other fetch paths use, and refuse a
  private/internal resolution (returns `None`, never dials);
- dial the *validated* IP (no second DNS lookup -> no rebinding window);
- never follow a redirect (any 3xx -> `None`);
- read at most 512 KB of body.

The fixtures are stdlib `http.server` servers on 127.0.0.1, port 0
(pytest-httpserver is not installed, ASSUMPTIONS D3). Loopback is only
reachable through the keyword-only `_allow_loopback_for_test=True`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scrape_core import robots

_CAP = 512 * 1024


def _serve(status: int, body: bytes, headers: dict[str, str] | None = None):
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            seen.append({"path": self.path, "host": self.headers.get("Host", "")})
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


@pytest.fixture
def http_server() -> Iterator:
    servers = []

    def start(status: int, body: bytes, headers: dict[str, str] | None = None):
        server, seen = _serve(status, body, headers)
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/robots.txt", seen

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_robots_refuses_private_resolution(monkeypatch):
    def deny(host, port):
        raise robots.UnsafeTargetError(host)

    monkeypatch.setattr(robots, "_resolve_validated_ip", deny)
    assert robots.default_robots_fetcher("http://evil.example/robots.txt") is None


def test_robots_real_guard_refuses_private_dns(monkeypatch):
    """No monkeypatched guard: the real validator rejects a host resolving to IMDS."""
    monkeypatch.setattr(robots, "_system_resolver", lambda host: ["169.254.169.254"])

    def must_not_dial(*args, **kwargs):
        raise AssertionError("dialled an unvalidated target")

    monkeypatch.setattr(robots, "_open_connection", must_not_dial)
    assert robots.default_robots_fetcher("http://evil.example/robots.txt") is None


def test_robots_refuses_loopback_without_test_flag(http_server):
    url, seen = http_server(200, b"User-agent: *\nDisallow: /x")
    assert robots.default_robots_fetcher(url) is None
    assert seen == []


def test_robots_dials_the_validated_ip(monkeypatch, http_server):
    """The connection goes to the IP the guard validated, with Host = hostname."""
    url, seen = http_server(200, b"User-agent: *\nDisallow: /x")
    port = int(url.split(":")[2].split("/")[0])
    monkeypatch.setattr(robots, "_resolve_validated_ip", lambda host, p: "127.0.0.1")
    body = robots.default_robots_fetcher(f"http://shop.example:{port}/robots.txt")
    assert body is not None and "Disallow" in body
    assert seen[0]["host"] == f"shop.example:{port}"


def test_robots_loopback_flag_is_keyword_only(http_server):
    url, _ = http_server(200, b"")
    with pytest.raises(TypeError):
        robots.default_robots_fetcher(url, "ua", True)  # type: ignore[misc]


def test_robots_does_not_follow_redirects(http_server):
    url, seen = http_server(302, b"", {"Location": "http://169.254.169.254/latest"})
    assert robots.default_robots_fetcher(url, _allow_loopback_for_test=True) is None
    assert len(seen) == 1


def test_robots_happy_path(http_server):
    url, _ = http_server(200, b"User-agent: *\nDisallow: /x")
    assert "Disallow" in robots.default_robots_fetcher(url, _allow_loopback_for_test=True)


def test_robots_body_capped(http_server):
    url, _ = http_server(200, b"a" * 2_000_000)
    body = robots.default_robots_fetcher(url, _allow_loopback_for_test=True)
    assert body is not None
    assert len(body) <= _CAP


def test_robots_non_2xx_is_none(http_server):
    url, _ = http_server(404, b"nope")
    assert robots.default_robots_fetcher(url, _allow_loopback_for_test=True) is None
