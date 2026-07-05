"""Unit test for per-rule error isolation in `run_refresh_pass` (SPEC-13 US3
T025, FR-021, `contracts/scheduler-loop.md` "Ordering & crash safety").

Exercises `apps.scheduler.app.scheduler.refresh.run_refresh_pass` against a
small purpose-built fake `session_factory`/session (no DB, no SQLAlchemy
engine) with `create_scope_job` monkeypatched to raise for one designated
"poison" rule. Unlike `tests/unit/test_create_scope_job.py`
(`FakeOrmSession`, which evaluates real `WHERE` clauses over seeded rows),
this fake session models the *claim* step directly as popping the head of
an ordered "due" pool -- `run_refresh_pass` never inspects the `Select` it
builds beyond `.scalars().first()`, so the fake only needs to honor that
shape while tracking commit/rollback per claimed rule.

Loaded in a fresh subprocess with `sys.path.insert(0, "apps/scheduler")`
ahead of the import -- mirrors `test_jobs_dispatch_task.py`'s
`_DISPATCH_TASK_CHECK` idiom: `apps/api` and `apps/scheduler` (like
`apps/workers`) each ship their own top-level ``app`` package, so a
plain `import app.scheduler.refresh` in the shared test process resolves
ambiguously to whichever ``app`` package another test module happened to
import first (in practice `apps/api`'s, since its editable `.pth` sorts
first) -- the explicit `sys.path` prepend inside the subprocess sidesteps
that collision instead of fighting it.

Asserts (FR-021 / US3 AS-1..4):
- A rule whose `create_scope_job` call raises rolls back **only its own**
  transaction (its `next_run_at`/`last_run_at`/`locked_at` stay unchanged,
  still due) -- the SAME code path a crash-before-commit would take
  (FR-014), not a second bespoke branch.
- An earlier rule that already committed in the same pass keeps its
  advanced `next_run_at` -- the poison rule's rollback does not undo it.
- The pass does not spin re-selecting the unchanged poison rule forever:
  `session_factory` is invoked a bounded, small number of times (claim A,
  claim poison, stop) even though `batch_limit` is much larger and a
  third due rule remains unclaimed in the pool.
- A later pass retries the still-due poison rule (still poisoned -> rolls
  back again) without ever re-selecting the earlier rule that already
  committed.
"""

from __future__ import annotations

import subprocess
import sys

_ISOLATION_CHECK = """
import sys
sys.path.insert(0, "apps/scheduler")

import uuid
from datetime import datetime, timedelta, timezone

import app.scheduler.refresh as refresh_module
from app.scheduler.refresh import run_refresh_pass
from app_shared.enums import ScrapeScope


class _FakeRule:
    # Duck-typed stand-in for RefreshRule -- only the attributes
    # run_refresh_pass/compute_next_run_at/_target_id_for_rule read.
    def __init__(self, *, rule_id, workspace_id, next_run_at):
        self.id = rule_id
        self.workspace_id = workspace_id
        self.enabled = True
        self.scope = ScrapeScope.WORKSPACE  # target_id resolves to None
        self.cron_expression = None
        self.interval_minutes = 15
        self.next_run_at = next_run_at
        self.last_run_at = None
        self.locked_at = None


class _FakeQueryResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None


class _FakeSession:
    # Claims the head of a shared ordered `pool` on execute(...),
    # ignoring the actual Select (real predicate/ordering/SKIP LOCKED
    # semantics are exercised by the live integration test, T026; this
    # fake only needs the same .execute(...).scalars().first() shape
    # run_refresh_pass calls).
    def __init__(self, pool, committed, rolled_back):
        self._pool = pool
        self._committed = committed
        self._rolled_back = rolled_back
        self._claimed = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, stmt):
        if not self._pool:
            return _FakeQueryResult([])
        self._claimed = self._pool[0]
        return _FakeQueryResult([self._claimed])

    def commit(self):
        assert self._claimed is not None
        self._committed.append(self._claimed.id)
        self._pool.remove(self._claimed)

    def rollback(self):
        if self._claimed is None:
            return
        self._rolled_back.append(self._claimed.id)
        # Rule stays at the head of the pool, fields untouched -- mirrors
        # a real ROLLBACK: next_run_at/last_run_at/locked_at unchanged,
        # SKIP-LOCKED lock released, still due.


def fail(label):
    print("FAIL:" + label)
    sys.exit(1)


# --- Scenario 1: poison rule rolls back alone; pass does not spin ----------

now = datetime.now(timezone.utc)
rule_a = _FakeRule(rule_id=uuid.uuid4(), workspace_id=uuid.uuid4(), next_run_at=now - timedelta(hours=1))
rule_poison = _FakeRule(rule_id=uuid.uuid4(), workspace_id=uuid.uuid4(), next_run_at=now - timedelta(minutes=30))
rule_c = _FakeRule(rule_id=uuid.uuid4(), workspace_id=uuid.uuid4(), next_run_at=now - timedelta(minutes=10))

pool = [rule_a, rule_poison, rule_c]  # already "ORDER BY next_run_at" ascending
committed = []
rolled_back = []
call_count = {"n": 0}


def session_factory():
    call_count["n"] += 1
    return _FakeSession(pool, committed, rolled_back)


poison_workspace_id = rule_poison.workspace_id


def fake_create_scope_job(session, *, workspace_id, **kwargs):
    if workspace_id == poison_workspace_id:
        raise RuntimeError("boom: simulated per-rule processing failure")
    return None, None


refresh_module.create_scope_job = fake_create_scope_job

# batch_limit is much larger than the due set -- if the pass were to
# re-select the unchanged poison rule instead of stopping, it would spin
# until batch_limit (or forever, if unbounded); the exact session_factory
# call-count assertion below proves it did not.
fired = run_refresh_pass(session_factory, now=now, batch_limit=100)

if fired != 1:
    fail("fired_not_1:" + str(fired))
if committed != [rule_a.id]:
    fail("committed_mismatch:" + str(committed))
if rule_a.last_run_at != now:
    fail("rule_a_last_run_at_wrong")
if rule_a.locked_at != now:
    fail("rule_a_locked_at_wrong")
if not (rule_a.next_run_at > now):
    fail("rule_a_next_run_at_not_advanced")

if rolled_back != [rule_poison.id]:
    fail("rolled_back_mismatch:" + str(rolled_back))
if rule_poison.last_run_at is not None:
    fail("poison_last_run_at_should_be_none")
if rule_poison.locked_at is not None:
    fail("poison_locked_at_should_be_none")
if rule_poison.next_run_at != now - timedelta(minutes=30):
    fail("poison_next_run_at_changed")

if rule_c.id in committed:
    fail("rule_c_should_not_have_fired")
if rule_c.last_run_at is not None:
    fail("rule_c_last_run_at_should_be_none")

if call_count["n"] != 2:
    fail("call_count_not_bounded:" + str(call_count["n"]))

# --- Scenario 2: a later pass retries the still-due poison rule without ----
# --- re-selecting the already-committed rule_a -----------------------------

fired_2 = run_refresh_pass(session_factory, now=now, batch_limit=100)

if fired_2 != 0:
    fail("second_pass_fired_not_0:" + str(fired_2))
if committed != [rule_a.id]:
    fail("second_pass_committed_changed:" + str(committed))
if rolled_back != [rule_poison.id, rule_poison.id]:
    fail("second_pass_rolled_back_wrong:" + str(rolled_back))
if rule_poison.next_run_at != now - timedelta(minutes=30):
    fail("poison_next_run_at_advanced_while_still_poisoned")

print("OK")
sys.exit(0)
"""


def test_poison_rule_isolated_and_pass_does_not_spin() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
