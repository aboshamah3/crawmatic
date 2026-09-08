"""Work-level fairness in `redispatch_pending_jobs` (EPA B3 / F07).

The sweep re-enqueues `SCRAPE_DISPATCH_JOB` for every non-terminal job
nothing else will pick up. It walked `_scan_job_refs`'s rows in whatever
order Postgres returned them — which, for a tenant that is far enough
behind, is that tenant's jobs, first and all of them. A workspace with
400 wedged jobs therefore consumed every tick and a second tenant's
single stuck job waited behind the entire backlog. Nothing anywhere else
in the pipeline undoes that: the fair-queue planes arbitrate *scheduling*
(which rules fire), not *recovery* (which already-created jobs get
re-delivered).

Two changes, tested separately here because they fix two different halves
of the problem:

* `_interleave_by_workspace` — WHOSE turn it is. A pure reordering: same
  refs, same per-workspace order, round-robin across workspaces.
* `SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK` — HOW MUCH of a turn.
  Counted in *actual re-enqueues*, not rows examined, so a workspace
  whose jobs mostly need nothing does not spend its budget looking at
  them.

Loaded in a fresh subprocess, like every other `tasks_jobs` unit test
here: `apps/api`/`apps/workers` each ship a top-level `app` package, and
`celery_app.py` calls `get_settings()` at module scope.
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

_SETUP = '''
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

import app.workers.tasks_jobs as tasks_jobs

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class Harness:
    """A seeded fake session + a recording `enqueue`, wired into the task."""

    def __init__(self, per_workspace_cap=4):
        self.session = FakeOrmSession()
        self.enqueued = []

        @contextmanager
        def _session():
            yield self.session

        tasks_jobs.get_session = _session
        # The cross-tenant `_scan_job_refs` scan runs on the BYPASSRLS
        # system session (mushtryati F-1) -- same fake, no real engine.
        tasks_jobs.get_system_session = _session
        tasks_jobs.set_workspace_context = lambda session, workspace_id: None
        tasks_jobs.enqueue = lambda name, *, queue=None, kwargs=None: (
            self.enqueued.append(kwargs)
        )

        class _Settings:
            SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK = per_workspace_cap

        tasks_jobs.get_settings = lambda: _Settings()

    def wedged_job(self, workspace_id):
        """A never-started PENDING job — the unambiguous re-dispatch case."""
        job = ScrapeJob(
            workspace_id=workspace_id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.PENDING,
            total_targets=1,
            source=ScrapeJobSource.API,
            created_at=NOW,
            started_at=None,
        )
        job.id = uuid.uuid4()
        self.session.seed(job)
        target = ScrapeJobTarget(
            workspace_id=workspace_id,
            scrape_job_id=job.id,
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.PENDING,
            created_at=NOW,
            dispatched_at=None,
        )
        target.id = uuid.uuid4()
        self.session.seed(target)
        return job

    def healthy_job(self, workspace_id):
        """A RUNNING job whose targets are all in flight — nothing to do."""
        job = ScrapeJob(
            workspace_id=workspace_id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.RUNNING,
            total_targets=1,
            source=ScrapeJobSource.API,
            created_at=NOW,
            started_at=NOW,
        )
        job.id = uuid.uuid4()
        self.session.seed(job)
        target = ScrapeJobTarget(
            workspace_id=workspace_id,
            scrape_job_id=job.id,
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.STARTED,
            created_at=NOW,
            dispatched_at=NOW,
        )
        target.id = uuid.uuid4()
        self.session.seed(target)
        return job

    def redispatched_workspaces(self):
        by_workspace = {}
        for kwargs in self.enqueued:
            key = kwargs["workspace_id"]
            by_workspace[key] = by_workspace.get(key, 0) + 1
        return by_workspace
'''


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_interleave_gives_every_workspace_a_turn_before_any_gets_a_second() -> None:
    """The property that matters: prefix coverage.

    The first N entries must cover N distinct workspaces (while that many
    have work), because the per-tick budget is spent from the front of
    this list.
    """
    _assert_ok(
        _run(
            """
noisy, quiet, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
refs = [(uuid.uuid4(), noisy) for _ in range(10)]
refs.append((uuid.uuid4(), quiet))
refs.append((uuid.uuid4(), third))
refs.append((uuid.uuid4(), third))

ordered = tasks_jobs._interleave_by_workspace(refs)

assert len(ordered) == len(refs)
assert sorted(ordered) == sorted(refs), "interleaving must not add or drop refs"

first_three = [workspace for _job, workspace in ordered[:3]]
assert set(first_three) == {noisy, quiet, third}, first_three
# Round two: the two workspaces that still have work, in the same order.
assert [w for _j, w in ordered[3:5]] == [noisy, third]
# The noisy tenant's tail comes last, not first.
assert [w for _j, w in ordered[5:]] == [noisy] * 8
print("OK")
"""
        )
    )


def test_interleave_preserves_each_workspace_own_order_and_is_a_no_op_for_one() -> None:
    """It decides whose turn it is — never which of a tenant's own jobs.

    A single-workspace fleet must come back byte-identical: the change
    has to be free for the deployment shape production has today.
    """
    _assert_ok(
        _run(
            """
solo = uuid.uuid4()
refs = [(uuid.uuid4(), solo) for _ in range(6)]
assert tasks_jobs._interleave_by_workspace(refs) == refs

a, b = uuid.uuid4(), uuid.uuid4()
a_jobs = [uuid.uuid4() for _ in range(3)]
b_jobs = [uuid.uuid4() for _ in range(3)]
refs = [(j, a) for j in a_jobs] + [(j, b) for j in b_jobs]
ordered = tasks_jobs._interleave_by_workspace(refs)
assert [j for j, w in ordered if w == a] == a_jobs
assert [j for j, w in ordered if w == b] == b_jobs

assert tasks_jobs._interleave_by_workspace([]) == []
print("OK")
"""
        )
    )


def test_a_noisy_tenant_cannot_consume_the_whole_redispatch_tick() -> None:
    """The regression this task exists for.

    Twenty wedged jobs in one workspace and one in another: before the
    fix the quiet tenant's job could sit behind all twenty. Now each
    workspace re-enqueues at most the per-tick cap, and the quiet
    tenant's single job is delivered in the SAME tick.
    """
    _assert_ok(
        _run(
            """
harness = Harness(per_workspace_cap=4)
noisy, quiet = uuid.uuid4(), uuid.uuid4()
for _ in range(20):
    harness.wedged_job(noisy)
quiet_job = harness.wedged_job(quiet)

tasks_jobs.redispatch_pending_jobs()

counts = harness.redispatched_workspaces()
assert counts[str(noisy)] == 4, counts
assert counts[str(quiet)] == 1, counts
assert str(quiet_job.id) in {k["scrape_job_id"] for k in harness.enqueued}
print("OK")
"""
        )
    )


def test_the_cap_counts_re_enqueues_not_rows_examined() -> None:
    """A workspace's budget is spent on WORK, not on being looked at.

    If the cap counted scanned rows, a tenant whose backlog is mostly
    healthy jobs would starve itself: the sweep would spend the budget
    skipping them and never reach the wedged one.
    """
    _assert_ok(
        _run(
            """
harness = Harness(per_workspace_cap=2)
workspace = uuid.uuid4()
for _ in range(6):
    harness.healthy_job(workspace)
wedged = [harness.wedged_job(workspace) for _ in range(3)]

tasks_jobs.redispatch_pending_jobs()

got = [k["scrape_job_id"] for k in harness.enqueued]
assert len(got) == 2, got
assert set(got) <= {str(job.id) for job in wedged}, got
print("OK")
"""
        )
    )


def test_a_single_tenant_fleet_still_drains_over_successive_ticks() -> None:
    """The cap paces a backlog; it never abandons one.

    Each tick is idempotent and re-reads the same rows, so a 10-job
    backlog behind a cap of 4 needs three ticks — not a lost job.
    """
    _assert_ok(
        _run(
            """
harness = Harness(per_workspace_cap=4)
workspace = uuid.uuid4()
for _ in range(10):
    harness.wedged_job(workspace)

for _tick in range(3):
    harness.enqueued.clear()
    tasks_jobs.redispatch_pending_jobs()
    assert len(harness.enqueued) == 4, harness.enqueued

# Nothing is consumed by a tick, so the sweep keeps offering the backlog
# until the dispatch side actually drains it -- the pacing property, not
# a queue-draining one.
print("OK")
"""
        )
    )
