"""Unit tests for `apps.workers.app.workers.tasks_strategy._select_sample_urls`
(PLAN_DOMAIN_STRATEGY_PROFILES.md §3/§4, discovery-gate fix, 2026-07-11).

Loaded in a fresh subprocess per scenario -- `apps/workers` ships its own
top-level `app` package, so importing `app.workers.*` in the shared test
process is ambiguous once another test module has already imported a
different `apps/*` member's `app` package (the same reason
`test_webhook_enqueue_seams.py` uses this pattern).

Exercises the `scope` branch directly against `_jobs_fake_session
.FakeOrmSession` (real `WHERE` evaluation, no DB) -- the exact bug this
fix closes: under `scope="domain"`, the `url_pattern` filter is dropped
so the sample gate no longer depends on stored per-product-slug match
patterns; `scope="url_pattern"` preserves the exact legacy filtered
behavior (regression pin).
"""

from __future__ import annotations

import os
import subprocess
import sys

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


def _run(script_body: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **_ENV}
    return subprocess.run(
        [sys.executable, "-c", script_body],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


_SETUP = """
import sys
import uuid

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _jobs_fake_session import FakeOrmSession
from app_shared.models.competitors_matches import CompetitorProductMatch

import app.workers.tasks_strategy as tasks_strategy

workspace_id = uuid.uuid4()
competitor_id = uuid.uuid4()
other_competitor_id = uuid.uuid4()


def _match(url, url_pattern, cid=competitor_id):
    return CompetitorProductMatch(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        competitor_id=cid,
        competitor_url=url,
        normalized_competitor_url=url,
        url_pattern=url_pattern,
        url_pattern_version=1,
    )


session = FakeOrmSession()
# Three matches, three distinct per-product-slug patterns (the exact
# "every product is its own n=1 pattern" bug scenario) -- plus one match
# on a different competitor that must never leak in.
session.seed(
    _match("https://noon.com/products/red-shoe-111", "https://noon.com/products/red-shoe-111"),
    _match("https://noon.com/products/blue-shoe-222", "https://noon.com/products/blue-shoe-222"),
    _match("https://noon.com/products/green-shoe-333", "https://noon.com/products/green-shoe-333"),
    _match(
        "https://jarir.com/products/other-999",
        "https://jarir.com/products/other-999",
        cid=other_competitor_id,
    ),
)
"""


def test_domain_scope_ignores_stored_url_pattern_and_gathers_all_competitor_matches() -> None:
    script = (
        _SETUP
        + """
urls = tasks_strategy._select_sample_urls(
    session,
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    url_pattern="https://noon.com/products/red-shoe-111",
    max_sample=10,
    scope="domain",
)

# All 3 same-competitor matches qualify despite 3 distinct stored
# url_pattern values -- the discovery-gate fix.
if len(urls) != 3:
    print("WRONG_COUNT:" + str(urls))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_url_pattern_scope_keeps_exact_legacy_filtered_behavior() -> None:
    script = (
        _SETUP
        + """
urls = tasks_strategy._select_sample_urls(
    session,
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    url_pattern="https://noon.com/products/red-shoe-111",
    max_sample=10,
    scope="url_pattern",
)

# Only the one match whose stored url_pattern matches exactly.
if urls != ["https://noon.com/products/red-shoe-111"]:
    print("WRONG_RESULT:" + str(urls))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_domain_scope_is_the_default() -> None:
    script = (
        _SETUP
        + """
urls = tasks_strategy._select_sample_urls(
    session,
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    url_pattern="https://noon.com/products/red-shoe-111",
    max_sample=10,
)

if len(urls) != 3:
    print("WRONG_COUNT:" + str(urls))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
