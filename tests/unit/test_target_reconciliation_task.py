"""Worker wiring for the safe, dry-run-first target reconciliation task."""

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


def test_task_defaults_to_preview_and_commits_only_reviewed_apply() -> None:
    script = r'''
import sys
import uuid
from contextlib import contextmanager

sys.path.insert(0, "apps/workers")

from app_shared.task_names import SCRAPE_RECONCILE_FALSE_FAILURES
import app.workers.tasks_jobs as tasks_jobs


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


session = FakeSession()


@contextmanager
def fake_get_session():
    yield session


calls = []


class FakeReport:
    def __init__(self, dry_run):
        self.dry_run = dry_run

    def as_dict(self):
        return {"dry_run": self.dry_run, "candidate_match_ids": []}


def fake_reconcile(session_arg, **kwargs):
    assert session_arg is session
    calls.append(kwargs)
    return FakeReport(kwargs["dry_run"])


tasks_jobs.get_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session_arg, workspace_id: None
tasks_jobs.reconcile_successful_failed_targets = fake_reconcile

workspace_id = str(uuid.uuid4())
job_id = str(uuid.uuid4())

try:
    tasks_jobs.reconcile_false_failed_targets.run(workspace_id, job_id, dry_run=False)
except ValueError as exc:
    assert "requested_by" in str(exc)
else:
    raise AssertionError("apply without an auditable requester was accepted")

preview = tasks_jobs.reconcile_false_failed_targets.run(workspace_id, job_id)
assert preview["dry_run"] is True
assert calls[-1]["expected_match_ids"] is None
assert session.commits == 0

expected = [str(uuid.uuid4())]
applied = tasks_jobs.reconcile_false_failed_targets.run(
    workspace_id,
    job_id,
    dry_run=False,
    expected_match_ids=expected,
    requested_by="operator@example.com",
)
assert applied["dry_run"] is False
assert calls[-1]["expected_match_ids"] == expected
assert session.commits == 1
assert tasks_jobs.app.conf.task_routes[SCRAPE_RECONCILE_FALSE_FAILURES] == {
    "queue": "maintenance"
}
print("OK")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env={**os.environ, **_ENV},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")
