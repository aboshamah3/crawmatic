"""`dispatch.generic_price_spider` (SPEC-07 thin task, `apps/workers/app/
workers/tasks_dispatch.py::dispatch_generic_price_spider`) unit test —
EPA C4b: the C3 grant this task authorizes under must be stamped onto the
Scrapyd schedule payload as `authorization_id`, exactly like the planner's
own `dispatch_job` batch loop (`test_jobs_dispatch_task.py`).

Fake session-free (this task never opens a DB session itself — the C3
gate is stubbed via `_costauth_test_stub`, the identical division of
labour `test_jobs_dispatch_task.py` uses): a stubbed HTTP transport wired
through the REAL, unchanged `ScrapydDispatchClient` (never a real
Redis/Scrapyd/network).

Loaded in a fresh subprocess for the same two reasons
`test_jobs_dispatch_task.py` documents: (1) `apps/api` and `apps/workers`
each ship their own top-level ``app`` package; (2) `celery_app.py` calls
`get_settings()` at module scope, needing a clean, self-contained env.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = """
import sys
sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import uuid

import requests

from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_dispatch as tasks_dispatch


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, *names):
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


calls = []


def fake_post(url, *, data, auth, timeout):
    calls.append({"url": url, "data": dict(data), "auth": auth})
    return FakeResponse(200, {"status": "ok", "jobid": "job-" + str(len(calls))})


fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_dispatch.ScrapydDispatchClient = client_factory
# EPA C3: this thin task authorizes before it POSTs -- stub the gate the
# same way `test_jobs_dispatch_task.py` does; the gate's own behaviour is
# proven in tests/integration/test_cost_authorization.py.
from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_dispatch)

workspace_id = str(uuid.uuid4())
scrape_job_id = str(uuid.uuid4())
match_id = str(uuid.uuid4())

jobid = tasks_dispatch.dispatch_generic_price_spider(
    workspace_id, scrape_job_id, [match_id], "HTTP", 0
)

if not jobid:
    print("NO_JOBID_RETURNED")
    sys.exit(1)

if len(calls) != 1:
    print("EXPECTED_ONE_SCHEDULE_CALL_GOT:" + str(len(calls)))
    sys.exit(1)

data = calls[0]["data"]
if "authorization_id" not in data:
    print("AUTHORIZATION_ID_MISSING")
    sys.exit(1)
try:
    uuid.UUID(data["authorization_id"])
except (ValueError, TypeError, AttributeError):
    print("AUTHORIZATION_ID_NOT_A_UUID_STRING:" + repr(data["authorization_id"]))
    sys.exit(1)

print("OK")
sys.exit(0)
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


def test_dispatch_generic_price_spider_forwards_its_grants_authorization_id() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- EPA Phase C F6: the denial and the release, at the FALLBACK site ----

_DENIAL_PRELUDE = """
import sys
sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import uuid

import requests

from app_shared.costauth import CostAuthorizationDenied
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_dispatch as tasks_dispatch


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, *names):
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


calls = []
fake_redis = FakeRedis()

workspace_id = str(uuid.uuid4())
scrape_job_id = str(uuid.uuid4())
match_id = str(uuid.uuid4())


def fail(message):
    print(message)
    sys.exit(1)
"""

# A denial here must RAISE, not return. This task's caller receives a
# jobid and would read `None`/`""` as "scheduled" — the exact
# looks-dispatched-never-happened outcome the gate exists to remove.
_FALLBACK_DENIAL_CHECK = _DENIAL_PRELUDE + """
from _costauth_test_stub import DenyingCostAuthorizationService, stub_cost_authorization


def fake_post(url, *, data, auth, timeout):
    calls.append({"url": url, "data": dict(data)})
    return FakeResponse(200, {"status": "ok", "jobid": "job-1"})


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_dispatch.ScrapydDispatchClient = client_factory
stub_cost_authorization(tasks_dispatch, DenyingCostAuthorizationService)

try:
    tasks_dispatch.dispatch_generic_price_spider(
        workspace_id, scrape_job_id, [match_id], "HTTP", 0
    )
except CostAuthorizationDenied as denial:
    if denial.reason.value != "MONEY_BUDGET_EXCEEDED":
        fail("WRONG_DENIAL_REASON_PROPAGATED:" + denial.reason.value)
else:
    fail("A_DENIED_FALLBACK_DISPATCH_RETURNED_INSTEAD_OF_RAISING")

if calls:
    fail("DENIED_FALLBACK_DISPATCH_STILL_POSTED:" + str(len(calls)))

print("OK")
sys.exit(0)
"""

_FALLBACK_RELEASE_CHECK = _DENIAL_PRELUDE + """
from _costauth_test_stub import AlwaysGrantCostAuthorizationService, stub_cost_authorization

released = []


class _RecordingCostAuth(AlwaysGrantCostAuthorizationService):
    def release(self, authorization_id, **kwargs):
        released.append(authorization_id)


def exploding_post(url, *, data, auth, timeout):
    raise requests.ConnectionError("scrapyd unreachable")


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = exploding_post
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_dispatch.ScrapydDispatchClient = client_factory
stub_cost_authorization(tasks_dispatch, _RecordingCostAuth)

try:
    tasks_dispatch.dispatch_generic_price_spider(
        workspace_id, scrape_job_id, [match_id], "HTTP", 0
    )
except Exception:
    pass

if len(released) != 1:
    fail("HOLD_WAS_NOT_RELEASED_AFTER_A_FAILED_POST:" + str(len(released)))

print("OK")
sys.exit(0)
"""


def _run(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_a_denied_fallback_dispatch_raises_and_posts_nothing() -> None:
    """EPA Phase C F6: the FALLBACK site's denial branch.

    Unlike the background sweeps, this task must RAISE: its caller reads
    the return value as a Scrapyd jobid, so a silently-returned ``None``
    would be indistinguishable from a successful schedule."""
    _run(_FALLBACK_DENIAL_CHECK)


def test_a_failed_fallback_post_returns_the_hold() -> None:
    """EPA Phase C F6: `costauth.release` on this site's `schedule()`
    exception path — the third and last of the three release sites."""
    _run(_FALLBACK_RELEASE_CHECK)
