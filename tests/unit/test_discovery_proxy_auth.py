"""`_proxy_url_with_credentials` — discovery proxy auth rides in the URL userinfo.

`requests` reaches an https:// target through a CONNECT tunnel, and anything
passed in `headers=` goes to the destination *inside* that tunnel, never to
the proxy. Discovery authenticated with a `Proxy-Authorization` header, so
every `PROXY_HTTP` probe died `407 NO_USER` (observed live 2026-08-04). A
failed probe is indistinguishable from "this method doesn't work here", so
discovery could never confirm a proxied method and kept promoting DIRECT
ones — which is why stech.ink held `DIRECT_HTTP` while accumulating 996
recorded failures.

Loaded in a fresh subprocess (same convention as `test_jobs_dispatch_task.py`):
`apps/api`/`apps/workers` each ship a top-level `app` package, and
`celery_app.py` calls `get_settings()` at module scope.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = """
import sys
sys.path.insert(0, "apps/workers")

from app.workers.tasks_strategy import _proxy_url_with_credentials as build

# 1. plain credentials land in the userinfo
got = build("http://gw.dataimpulse.com:823", "user", "pw")
assert got == "http://user:pw@gw.dataimpulse.com:823", got

# 2. a DataImpulse-style username (`__cr.sa`, `;sessid.`) and a password
#    containing :/@// must be percent-encoded so they cannot split the URL
got = build("http://gw.dataimpulse.com:823", "login__cr.sa;sessid.ab12", "p:a@ss/w")
assert got == "http://login__cr.sa%3Bsessid.ab12:p%3Aa%40ss%2Fw@gw.dataimpulse.com:823", got
assert got.count("@") == 1, got

# 3. a bare host:port gets the http scheme
got = build("gw.dataimpulse.com:823", "u", "p")
assert got == "http://u:p@gw.dataimpulse.com:823", got

# 4. re-running on an already-credentialed URL replaces, never nests
once = build("http://gw.dataimpulse.com:823", "u1", "p1")
got = build(once, "u2", "p2")
assert got == "http://u2:p2@gw.dataimpulse.com:823", got

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


def test_proxy_credentials_go_in_the_url_not_a_header() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
