"""Every registered worker task name must point at the function it names.

2026-10-02: 44b24ac inserted the helper `_denial_window_expired` between
`@app.task(name=SCRAPE_DISPATCH_JOB)` and `def dispatch_job`, so the
decorators registered the HELPER under `scrape_dispatch.dispatch_job`.
Every dispatch then raised `TypeError: _denial_window_expired() got an
unexpected keyword argument 'workspace_id'`, prod scraped nothing for a
day and a half, and no unit test noticed: they all call `dispatch_job`
directly, which still worked as a plain function.

Subprocess for the same reasons as `test_dispatch_routing.py` (two
top-level ``app`` packages; `celery_app.py` reads settings at import).
"""

from __future__ import annotations

import os
import subprocess
import sys

_REGISTRY_CHECK = """
import inspect
import sys

sys.path.insert(0, "apps/workers")

import app.workers.tasks_analysis
import app.workers.tasks_dispatch
import app.workers.tasks_jobs
import app.workers.tasks_maintenance
import app.workers.tasks_outbox
import app.workers.tasks_strategy
import app.workers.tasks_webhooks
from app_shared.task_names import SCRAPE_DISPATCH_JOB
from app.workers.celery_app import app as celery

bad = []
for name, task in sorted(celery.tasks.items()):
    if name.startswith("celery."):
        continue
    fn = getattr(task, "run", None)
    fn_name = getattr(fn, "__name__", "")
    if fn_name.startswith("_"):
        bad.append(f"{name} -> private helper {fn_name}")

dispatch = celery.tasks[SCRAPE_DISPATCH_JOB].run
if dispatch.__name__ != "dispatch_job":
    bad.append(f"{SCRAPE_DISPATCH_JOB} -> {dispatch.__name__}")
params = list(inspect.signature(dispatch).parameters)
if params != ["scrape_job_id", "workspace_id"]:
    bad.append(f"{SCRAPE_DISPATCH_JOB} signature {params}")

if bad:
    print("\\n".join(bad))
    sys.exit(1)
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


def test_registered_tasks_point_at_their_own_functions() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRY_CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
