"""Per-rule error isolation in `run_refresh_pass` (SPEC-13 US3 T025, FR-021;
rewritten by EPA B3 / F07, 2026-09-07).

Exercises `apps.scheduler.app.scheduler.refresh.run_refresh_pass` against a
small purpose-built fake `session_factory`/session (no DB, no SQLAlchemy
engine) with `create_scope_job` monkeypatched to raise for one designated
"poison" rule. Unlike `tests/unit/test_create_scope_job.py`
(`FakeOrmSession`, which evaluates real `WHERE` clauses over seeded rows),
this fake models the claim step as "the first DUE rule in an ordered
pool" — it evaluates the two predicates that decide due-ness (`enabled`,
`next_run_at <= now`) and nothing else, because those two are exactly what
the isolation property now turns on.

## What changed, and why the old assertion was wrong

Until B3 this module pinned the OPPOSITE behaviour: a failure ended the
pass (`break`), and the test asserted `session_factory` was called
exactly twice — "claim A, claim poison, stop". The mechanism behind that
`break` was real: with `next_run_at` untouched by the rollback, the same
poison rule is re-selected by the identical claim query and the pass
spins forever.

But stopping is a cure worse than the disease. It means ONE bad rule
stops EVERY other tenant's scheduling for a full poll interval — the
exact "one poison item aborts the pass" failure the fair queue exists to
remove, still present in the fallback path. F07 fixes the spin at its
cause instead: `record_rule_failure` increments `consecutive_failures`
and pushes `next_run_at` out `min(2**n x 60s, 6h)` in its OWN
transaction, so the failed rule leaves the due window and the loop
continues to the next rule. That is what this module now asserts.

Asserts (FR-021 / US3 AS-1..4, as amended by F07):
- A rule whose `create_scope_job` call raises rolls back **only its own**
  transaction — the SAME code path a crash-before-commit would take
  (FR-014), not a second bespoke branch. `last_run_at`/`locked_at` stay
  unset: it did not run.
- Its failure is then recorded DURABLY in a separate transaction:
  `consecutive_failures` goes up and `next_run_at` moves past `now`.
- **The pass continues.** A due rule ordered AFTER the poison one still
  fires in the same pass — this is the assertion that fails against the
  pre-B3 `break`.
- An earlier rule that already committed keeps its advanced
  `next_run_at`; the poison rule's rollback does not undo it.
- The pass does not spin: `session_factory` is called a bounded, small
  number of times even though `batch_limit` is far larger.
- A later pass does NOT retry the backed-off rule (it is no longer due),
  and retries it once the backoff has elapsed — with a longer backoff the
  second time.

Loaded in a fresh subprocess with `sys.path.insert(0, "apps/scheduler")`
ahead of the import -- mirrors `test_jobs_dispatch_task.py`'s
`_DISPATCH_TASK_CHECK` idiom: `apps/api` and `apps/scheduler` (like
`apps/workers`) each ship their own top-level ``app`` package, so a
plain `import app.scheduler.refresh` in the shared test process resolves
ambiguously to whichever ``app`` package another test module happened to
import first (in practice `apps/api`'s, since its editable `.pth` sorts
first) -- the explicit `sys.path` prepend inside the subprocess sidesteps
that collision instead of fighting it.
"""

from __future__ import annotations

import subprocess
import sys

_ISOLATION_CHECK = """
import sys
sys.path.insert(0, "apps/scheduler")

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.sql import operators as sa_operators

import app.scheduler.refresh as refresh_module
from app.scheduler.refresh import run_refresh_pass
from app_shared.enums import ScrapeScope


class _FakeRule:
    # Duck-typed stand-in for RefreshRule -- only the attributes
    # run_refresh_pass/compute_next_run_at/_target_id_for_rule/
    # record_rule_failure read.
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
        self.consecutive_failures = 0
        self.last_failure_at = None
        self.last_failure_error = None


def _eval(clause, obj):
    # The two predicates that decide due-ness, evaluated for real off the
    # statement the production code built -- `.where(RefreshRule.enabled,
    # RefreshRule.next_run_at <= now)` and the by-id lookup
    # `record_rule_failure` issues.
    subclauses = getattr(clause, "clauses", None)
    if subclauses is not None:
        return all(_eval(sub, obj) for sub in subclauses)
    operator_ = getattr(clause, "operator", None)
    if operator_ is None:
        return bool(getattr(obj, clause.name))
    if not hasattr(clause, "left"):
        value = bool(getattr(obj, clause.element.name))
        return value if operator_ is sa_operators.is_true else not value
    left = getattr(obj, clause.left.name)
    right = getattr(clause.right, "value", clause.right)
    if operator_ is sa_operators.eq:
        return left == right
    if operator_ is sa_operators.le:
        return left is not None and left <= right
    raise NotImplementedError(repr(operator_))


class _FakeQueryResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None


class _FakeSession:
    # Claims the first MATCHING rule of a shared ordered `pool` (the pool
    # is already in `ORDER BY next_run_at` order), evaluating the
    # statement's own WHERE clause so a rule backed off out of the due
    # window really stops being claimable.
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
        where = stmt.whereclause
        matched = [r for r in self._pool if where is None or _eval(where, r)]
        self._claimed = matched[0] if matched else None
        return _FakeQueryResult(matched)

    def commit(self):
        assert self._claimed is not None
        self._committed.append(self._claimed.id)

    def rollback(self):
        if self._claimed is None:
            return
        self._rolled_back.append(self._claimed.id)
        # Rule stays in the pool, fields untouched -- mirrors a real
        # ROLLBACK: next_run_at/last_run_at/locked_at unchanged,
        # SKIP-LOCKED lock released, still due.

    def close(self):
        pass


def fail(label):
    print("FAIL:" + label)
    sys.exit(1)


# --- Scenario 1: poison rule is isolated AND the pass keeps going ---------

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
# re-select the backed-off poison rule it would spin until the failure
# bound (100); the call-count assertion below proves it did not.
fired = run_refresh_pass(session_factory, now=now, batch_limit=100)

# THE B3 assertion: the pass did not stop at the poison rule.
if fired != 2:
    fail("fired_not_2:" + str(fired))
# Three commits, in order: rule_a fired, the poison rule's DURABLE
# failure record (its own transaction -- that is the point), rule_c
# fired. The middle one is why "committed" is not the same list as
# "fired".
if committed != [rule_a.id, rule_poison.id, rule_c.id]:
    fail("committed_mismatch:" + str(committed))
if rule_a.last_run_at != now or rule_a.locked_at != now:
    fail("rule_a_clock_not_advanced")
if not (rule_a.next_run_at > now):
    fail("rule_a_next_run_at_not_advanced")
if rule_c.last_run_at != now:
    fail("rule_c_should_have_fired_after_the_poison_rule")
if not (rule_c.next_run_at > now):
    fail("rule_c_next_run_at_not_advanced")

# Only the poison rule's own transaction was undone.
if rolled_back != [rule_poison.id]:
    fail("rolled_back_mismatch:" + str(rolled_back))
if rule_poison.last_run_at is not None:
    fail("poison_last_run_at_should_be_none")
if rule_poison.locked_at is not None:
    fail("poison_locked_at_should_be_none")

# ...and its failure was recorded durably, out of the due window.
if rule_poison.consecutive_failures != 1:
    fail("poison_failures_not_counted:" + str(rule_poison.consecutive_failures))
if rule_poison.next_run_at != now + timedelta(seconds=120):
    fail("poison_backoff_wrong:" + str(rule_poison.next_run_at))
if rule_poison.last_failure_at != now:
    fail("poison_last_failure_at_not_set")
if "boom" not in (rule_poison.last_failure_error or ""):
    fail("poison_error_not_recorded:" + str(rule_poison.last_failure_error))

# Bounded: claim A, claim poison, record the failure, claim C, claim
# nothing. Never a spin.
if not (0 < call_count["n"] <= 8):
    fail("call_count_not_bounded:" + str(call_count["n"]))

# --- Scenario 2: a backed-off rule is not retried until it is due again ---

before = call_count["n"]
fired_2 = run_refresh_pass(session_factory, now=now, batch_limit=100)

if fired_2 != 0:
    fail("second_pass_fired_not_0:" + str(fired_2))
if rolled_back != [rule_poison.id]:
    fail("second_pass_retried_a_backed_off_rule:" + str(rolled_back))
if rule_poison.consecutive_failures != 1:
    fail("second_pass_counted_a_failure_that_did_not_happen")
if call_count["n"] - before != 1:
    fail("second_pass_should_be_one_empty_claim:" + str(call_count["n"] - before))

# --- Scenario 3: once the backoff elapses it IS retried, and backs off ----
# --- further -------------------------------------------------------------

later = rule_poison.next_run_at + timedelta(seconds=1)
fired_3 = run_refresh_pass(session_factory, now=later, batch_limit=100)

if fired_3 != 0:
    fail("third_pass_fired_not_0:" + str(fired_3))
if rolled_back != [rule_poison.id, rule_poison.id]:
    fail("third_pass_did_not_retry:" + str(rolled_back))
if rule_poison.consecutive_failures != 2:
    fail("third_pass_failures_not_2:" + str(rule_poison.consecutive_failures))
if rule_poison.next_run_at != later + timedelta(seconds=240):
    fail("third_pass_backoff_did_not_grow:" + str(rule_poison.next_run_at))

print("OK")
sys.exit(0)
"""


def test_poison_rule_isolated_and_pass_continues() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
