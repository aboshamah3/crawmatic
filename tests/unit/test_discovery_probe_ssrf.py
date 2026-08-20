"""Discovery probe fetches run the fetch-time SSRF guard (audit H7).

`strategy_discovery_run` is the one path in this system that fetches URLs
off-reactor, with plain `requests`, instead of through a Scrapy spider.
Every spider fetch is resolve-then-checked at fetch time --
`scrape_core.safety.resolver.SafeResolver` for the HTTP project,
`scrape_core.browser.ssrf.abort_unsafe_request`
(`PLAYWRIGHT_ABORT_REQUEST`) for the browser one -- while these probes
re-ran only the **save-time** `validate_competitor_url`, which by its own
docstring performs no DNS resolution at all. A hostname that resolves
public at save time and private at fetch time (DNS rebinding), or a
public URL that 302s into the internal network, was fetched.

Both legs (`_fetch_direct`, `_fetch_via_proxy`) now go through
`_probe_get`, which reuses `validate_resolved_target` -- the same
function behind both spider guards.

Run in a fresh subprocess (the convention `test_discovery_proxy_auth.py`
established): `apps/api`/`apps/workers` each ship a top-level `app`
package and `celery_app.py` calls `get_settings()` at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = """
import sys
sys.path.insert(0, "apps/workers")

import requests

from app.workers import tasks_strategy as ts

# --- 1. a host resolving to a private address is refused, no fetch made ---

fetched = []
def _boom(*args, **kwargs):
    fetched.append(args)
    raise AssertionError("requests.get must never be reached for an unsafe target")
ts.requests.get = _boom

ts._probe_resolver = lambda host: ["10.0.0.7"]
assert ts._fetch_direct("https://rebind.example.com/p/1", retry=False) is None, "direct leg"
assert ts._fetch_direct("https://rebind.example.com/p/1", retry=True) is None, "retry leg"
assert fetched == [], fetched

# link-local (cloud metadata) is refused too
ts._probe_resolver = lambda host: ["169.254.169.254"]
assert ts._probe_get("https://metadata.example.com/") is None
assert fetched == [], fetched

# loopback
ts._probe_resolver = lambda host: ["127.0.0.1"]
assert ts._probe_get("https://localhost-alias.example.com/") is None
assert fetched == [], fetched

# --- 2. fail closed: an unresolvable host is not "safe" -------------------

import socket
def _gaierror(host):
    raise socket.gaierror(-2, "Name or service not known")
ts._probe_resolver = _gaierror
assert ts._probe_get("https://nx.example.com/") is None
assert fetched == [], fetched

# --- 3. a public host IS fetched (the guard is not a blanket refusal) -----

class _Resp:
    def __init__(self, status_code=200, text="<html>ok</html>", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.is_redirect = status_code in (301, 302, 303, 307, 308) and "location" in self.headers

calls = []
def _ok(url, **kwargs):
    calls.append(url)
    assert kwargs.get("allow_redirects") is False, kwargs
    return _Resp()
ts.requests.get = _ok
ts._probe_resolver = lambda host: ["93.184.216.34"]
assert ts._fetch_direct("https://shop.example.com/p/1", retry=False) == "<html>ok</html>"
assert calls == ["https://shop.example.com/p/1"], calls

# --- 4. every redirect hop is re-validated ------------------------------

hops = []
def _redirect_then_private(url, **kwargs):
    hops.append(url)
    if url == "https://shop.example.com/p/1":
        return _Resp(302, "", {"location": "https://internal.example.com/admin"})
    raise AssertionError("the private redirect target must never be fetched")

def _resolver(host):
    return ["93.184.216.34"] if host == "shop.example.com" else ["10.1.2.3"]

ts.requests.get = _redirect_then_private
ts._probe_resolver = _resolver
assert ts._fetch_direct("https://shop.example.com/p/1", retry=False) is None
assert hops == ["https://shop.example.com/p/1"], hops

# a redirect to another PUBLIC host is followed normally
def _redirect_then_public(url, **kwargs):
    hops.append(url)
    if url == "https://shop.example.com/p/1":
        return _Resp(302, "", {"location": "https://cdn.example.com/p/1"})
    return _Resp(200, "<html>followed</html>")

hops.clear()
ts.requests.get = _redirect_then_public
ts._probe_resolver = lambda host: ["93.184.216.34"]
assert ts._fetch_direct("https://shop.example.com/p/1", retry=False) == "<html>followed</html>"
assert hops == ["https://shop.example.com/p/1", "https://cdn.example.com/p/1"], hops

# --- 5. the proxied leg is guarded too ----------------------------------

ts._build_proxy_kwargs = lambda session, workspace_id: {"proxies": {}, "headers": {}}
ts.paid_requests_allowed = lambda *a, **k: (True, None)
ts.requests.get = _boom
ts._probe_resolver = lambda host: ["192.168.5.5"]
import uuid
assert ts._fetch_via_proxy(None, uuid.uuid4(), "https://rebind.example.com/p/1") is None
assert fetched == [], fetched

print("OK")
"""

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_probe_fetches_refuse_a_host_resolving_to_a_private_address() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")
