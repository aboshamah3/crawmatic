"""The strategy probe dials the IP its SSRF guard validated (audit E2).

`_probe_get` already resolve-then-checks every hop with
`scrape_core.safety.fetch.validate_resolved_target` (audit H7), but it
then handed the *hostname* to `requests.get`, which resolved it a second
time. A DNS-rebinding host can answer the guard with a public address and
the connect with `127.0.0.1` / `169.254.169.254` -- the classic TOCTOU.

The probe now sends each hop through a `requests.Session` with a
`PinnedHTTPAdapter`: the socket goes to the validated IP, while `Host`,
TLS SNI and certificate verification stay on the original hostname.

Proof strategy: a fake resolver answers PUBLIC on the first lookup of a
host and `127.0.0.1` on every later one. urllib3's dial seam
(`urllib3.util.connection.create_connection`, the function urllib3 calls
to open every socket) is monkeypatched to record the address it was asked
for and then connect to a local fixture server. Every recorded address
must be the validated public IP; the resolver must be asked exactly once
per hop.

Run in a fresh subprocess (the convention `test_discovery_probe_ssrf.py`
established): `apps/api`/`apps/workers` each ship a top-level `app`
package and `celery_app.py` calls `get_settings()` at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = r"""
import sys, os, ssl, socket, subprocess, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, "apps/workers")

import requests
import urllib3.util.connection as u3conn

from app.workers import tasks_strategy as ts

# --- fixture server --------------------------------------------------------
seen = []  # (Host header, path)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append((self.headers.get("Host"), self.path))
        if self.path == "/hop1":
            self.send_response(302)
            self.send_header("Location", "http://cdn.example.com/p/2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<html>pinned</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

def serve(tls_context=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context is not None:
        srv.socket = tls_context.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# --- dial recorder: urllib3's one socket-opening seam ----------------------
dialed = []
_real_create_connection = u3conn.create_connection
target = {"port": None}

def recording_create_connection(address, *args, **kwargs):
    dialed.append(address)
    return _real_create_connection(("127.0.0.1", target["port"]), *args, **kwargs)

u3conn.create_connection = recording_create_connection

# --- rebinding resolver: public first, 127.0.0.1 afterwards ----------------
PUBLIC = {"shop.example.com": "93.184.216.34", "cdn.example.com": "93.184.216.35"}
lookups = []

def rebinding_resolver(host):
    lookups.append(host)
    if lookups.count(host) == 1:
        return [PUBLIC[host]]
    return ["127.0.0.1"]

ts._probe_resolver = rebinding_resolver

# === 1. plain HTTP: the socket goes to the validated IP, Host stays =========
srv = serve()
target["port"] = srv.server_address[1]

resp = ts._probe_get("http://shop.example.com/p/1", headers=ts._PROBE_HEADERS)
assert resp is not None and resp.status_code == 200, resp
assert resp.text == "<html>pinned</html>", resp.text
assert dialed == [("93.184.216.34", 80)], dialed
assert seen == [("shop.example.com", "/p/1")], seen
assert lookups == ["shop.example.com"], lookups

# a SECOND probe of the same host: the guard now sees 127.0.0.1 and refuses
dialed.clear(); seen.clear()
assert ts._probe_get("http://shop.example.com/p/1") is None
assert dialed == [], dialed
assert seen == [], seen

# === 2. every redirect hop is pinned to ITS validated IP ===================
lookups.clear(); dialed.clear(); seen.clear()
resp = ts._probe_get("http://shop.example.com:8080/hop1")
assert resp is not None and resp.text == "<html>pinned</html>", resp
assert dialed == [("93.184.216.34", 8080), ("93.184.216.35", 80)], dialed
assert seen == [("shop.example.com:8080", "/hop1"), ("cdn.example.com", "/p/2")], seen
assert lookups == ["shop.example.com", "cdn.example.com"], lookups
srv.shutdown()

# === 3. HTTPS: dial the IP, SNI + certificate verified on the hostname =====
tmp = tempfile.mkdtemp()
def make_cert(cn):
    key, crt = os.path.join(tmp, cn + ".key"), os.path.join(tmp, cn + ".crt")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=" + cn, "-addext", "subjectAltName=DNS:" + cn,
         "-keyout", key, "-out", crt],
        check=True, capture_output=True,
    )
    return key, crt

key, crt = make_cert("shop.example.com")
sni_seen = []
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(crt, key)
ctx.sni_callback = lambda sslsock, name, c: sni_seen.append(name)
srv = serve(ctx)
target["port"] = srv.server_address[1]

lookups.clear(); dialed.clear(); seen.clear()
resp = ts._probe_get("https://shop.example.com/p/1", verify=crt)
assert resp is not None and resp.text == "<html>pinned</html>", resp
assert dialed == [("93.184.216.34", 443)], dialed
assert sni_seen == ["shop.example.com"], sni_seen
assert seen == [("shop.example.com", "/p/1")], seen
srv.shutdown()

# a certificate for ANOTHER name is rejected: verification is on the hostname,
# never on the IP and never disabled
key2, crt2 = make_cert("evil.example.com")
ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx2.load_cert_chain(crt2, key2)
srv = serve(ctx2)
target["port"] = srv.server_address[1]
lookups.clear(); dialed.clear()
try:
    ts._probe_get("https://shop.example.com/p/1", verify=crt2)
except requests.exceptions.SSLError:
    pass
else:
    raise AssertionError("a certificate for another hostname must fail verification")
assert dialed == [("93.184.216.34", 443)], dialed
srv.shutdown()

# === 4. adapter pool wiring (HTTPS) ========================================
adapter = ts.PinnedHTTPAdapter("shop.example.com", "93.184.216.34")
prepared = requests.Request("GET", "https://shop.example.com/x").prepare()
pool = adapter.get_connection_with_tls_context(prepared, True)
assert pool.host == "93.184.216.34", pool.host
assert pool.assert_hostname == "shop.example.com", pool.assert_hostname
assert pool.conn_kw.get("server_hostname") == "shop.example.com", pool.conn_kw

# a different host is never pinned to this hop's IP
other = requests.Request("GET", "https://other.example.com/x").prepare()
assert adapter.get_connection_with_tls_context(other, True).host == "other.example.com"

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


def test_probe_connects_to_the_validated_ip_on_every_hop() -> None:
    env = {**os.environ, **_ENV}
    # the fixture server is local; never route the probe through a proxy
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")
