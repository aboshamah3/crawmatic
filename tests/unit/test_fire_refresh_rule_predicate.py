"""`fire_refresh_rule`'s two claims: the due-time recheck and the occurrence key.

EPA B3 (F07). `FOR UPDATE SKIP LOCKED` on the rule row settles the
*simultaneous* race — two replicas reaching for the same row in the same
instant. It never settled the *sequential* one, which is the shape that
actually produces duplicate scrape jobs:

    replica A loads the due candidate list          (rule R is due)
    replica B fires R and advances R.next_run_at    (R is no longer due)
    replica A takes the lock on R                   (nobody holds it)
    replica A fires R again                         <-- duplicate

Two independent guards close it, and this module tests both against the
REAL statements the production code builds (the fake session below
evaluates SQLAlchemy's own clause objects rather than being told what to
return):

1. **The recheck.** `next_run_at <= now` is part of the LOCKING select,
   so A's second attempt matches zero rows and returns `False`.
2. **The occurrence key.** `(rule_id, scheduled_for)` is INSERTed into
   `refresh_rule_occurrences` *before* `create_scope_job` runs; the
   primary key is the claim. A second INSERT for the same occurrence
   raises `IntegrityError` -> rollback -> `False`, whatever the two
   transactions' interleaving, process, or restart.

Guard 1 without guard 2 still loses to clock skew or a long-enough pause
between the two statements. Guard 2 without guard 1 turns every stale
candidate into a wasted INSERT-and-rollback. The tests below therefore
defeat each guard separately (a rule advanced before the claim; a rule
whose clock is wound BACK to an occurrence already recorded) and assert
`False` and "no job created" either way.

Loaded in a fresh subprocess with `sys.path.insert(0, "apps/scheduler")`
ahead of the import — the same idiom as `test_refresh_pass_isolation.py`
and `test_scheduler_durable_cadence.py`: `apps/api`, `apps/workers` and
`apps/scheduler` each ship their own top-level `app` package, so a bare
`import app.scheduler.scheduler_app` in the shared test process resolves
to whichever one another module imported first.
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
sys.path.insert(0, "apps/scheduler")

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.dml import Insert, Update

from app.scheduler import scheduler_app
from app_shared.enums import ScrapeScope
from app_shared.models.refresh_rules import RefreshRule

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
OCC = datetime(2026, 9, 7, 11, 45, 0, tzinfo=timezone.utc)


def _eval(clause, obj):
    """Evaluate one real SQLAlchemy WHERE clause against a plain object.

    Deliberately evaluates the clause the production code BUILT rather
    than matching on its text: a test that greps for "next_run_at <= now"
    passes against a predicate that is present but wired to the wrong
    operand.
    """
    subclauses = getattr(clause, "clauses", None)
    if subclauses is not None:
        results = [_eval(sub, obj) for sub in subclauses]
        if clause.operator is sa_operators.and_:
            return all(results)
        if clause.operator is sa_operators.or_:
            return any(results)
        raise NotImplementedError(repr(clause.operator))
    operator_ = getattr(clause, "operator", None)
    if operator_ is None:
        return bool(getattr(obj, clause.name))
    if not hasattr(clause, "left"):
        # A bare boolean column used as a predicate --
        # `.where(RefreshRule.enabled)` compiles to an `AsBoolean` wrapper
        # around the column, not to a binary comparison.
        value = bool(getattr(obj, clause.element.name))
        return value if operator_ is sa_operators.is_true else not value
    left = getattr(obj, clause.left.name)
    right = getattr(clause.right, "value", clause.right)
    if operator_ is sa_operators.eq:
        return left == right
    if operator_ is sa_operators.le:
        return left is not None and left <= right
    raise NotImplementedError(repr(operator_))


class _Occurrence:
    def __init__(self, rule_id, scheduled_for, fired_at):
        self.rule_id = rule_id
        self.scheduled_for = scheduled_for
        self.fired_at = fired_at
        self.scrape_job_id = None


class FakeSession:
    """A Session double with a real primary key on the occurrence table.

    `refresh_rule_occurrences` is modelled as a dict keyed by
    `(rule_id, scheduled_for)` -- the actual composite primary key -- so a
    duplicate INSERT raises the same `IntegrityError` Postgres would.
    Everything else (the locking select, the UPDATE that stamps
    `scrape_job_id`) is evaluated against the statements the code really
    builds.
    """

    def __init__(self, rules):
        self.rules = list(rules)
        self.occurrences = {}
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt):
        if isinstance(stmt, Insert):
            params = stmt.compile().params
            key = (params["rule_id"], params["scheduled_for"])
            if key in self.occurrences:
                raise IntegrityError(
                    "INSERT INTO refresh_rule_occurrences", {}, Exception(
                        'duplicate key value violates unique constraint '
                        '"pk_refresh_rule_occurrences"'
                    )
                )
            self.occurrences[key] = _Occurrence(key[0], key[1], params["fired_at"])
            return None
        if isinstance(stmt, Update):
            values = {k: getattr(v, "value", v) for k, v in stmt._values.items()}
            for occurrence in self.occurrences.values():
                if _eval(stmt.whereclause, occurrence):
                    for name, value in values.items():
                        setattr(occurrence, name, value)
            return None
        where = stmt.whereclause
        matched = [r for r in self.rules if where is None or _eval(where, r)]
        return _Result(matched)

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None


def make_rule(*, next_run_at=OCC, enabled=True):
    rule = RefreshRule(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="every 15m",
        scope=ScrapeScope.WORKSPACE,
        interval_minutes=15,
        cron_expression=None,
        priority=0,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    rule.consecutive_failures = 0
    return rule


class _RecordingCreateScopeJob:
    def __init__(self, job_id=None, raises=None):
        self.calls = []
        self.job_id = job_id or uuid.uuid4()
        self.raises = raises

    def __call__(self, session, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.job_id, "PENDING"


def install(create_scope_job):
    scheduler_app.create_scope_job = create_scope_job
    return create_scope_job
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


def test_fire_rechecks_due_time_at_claim() -> None:
    """Guard 1, the plan's first Step-1 test.

    The candidate was loaded while due; another process advanced
    `next_run_at` before we took the lock. The recheck lives in the
    LOCKING select, so the row simply does not match — no job, no
    occurrence, `False`.
    """
    _assert_ok(
        _run(
            """
create = install(_RecordingCreateScopeJob())
# Loaded as a due candidate at OCC, then advanced past NOW by a peer.
rule = make_rule(next_run_at=NOW + timedelta(minutes=15))
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is False
assert create.calls == [], create.calls
assert session.occurrences == {}
assert session.commits == 0
print("OK")
"""
        )
    )


def test_occurrence_identity_is_unique() -> None:
    """Guard 2, the plan's second Step-1 test.

    The first firing claims `(rule, OCC)`. A stale candidate from a second
    scheduler then winds the rule's clock BACK to OCC — defeating the
    recheck completely, which is the point: the recheck is not what makes
    this safe. The occurrence primary key is, and it refuses the second
    firing.
    """
    _assert_ok(
        _run(
            """
create = install(_RecordingCreateScopeJob())
rule = make_rule(next_run_at=OCC)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is True
assert len(create.calls) == 1
assert list(session.occurrences) == [(rule.id, OCC)]

# The stale second scheduler: same rule, same occurrence, clock rewound.
rule.next_run_at = OCC
assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is False
assert len(create.calls) == 1, create.calls
assert len(session.occurrences) == 1
assert session.rollbacks == 1
print("OK")
"""
        )
    )


def test_the_occurrence_row_is_written_before_the_job_is_created() -> None:
    """Ordering is the guarantee, not an implementation detail.

    If the job were created first, a crash between the two writes would
    leave a job whose occurrence was never claimed — and the next pass
    would create a second one. Asserted by observing the occurrence table
    from inside `create_scope_job` itself.
    """
    _assert_ok(
        _run(
            """
seen = {}


class _Observing(_RecordingCreateScopeJob):
    def __call__(self, session, **kwargs):
        seen["occurrences"] = dict(session.occurrences)
        return super().__call__(session, **kwargs)


create = install(_Observing())
rule = make_rule(next_run_at=OCC)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is True
assert list(seen["occurrences"]) == [(rule.id, OCC)], seen
print("OK")
"""
        )
    )


def test_the_created_job_id_is_stamped_onto_the_occurrence() -> None:
    """The ledger is an audit trail, so it records what the firing produced."""
    _assert_ok(
        _run(
            """
job_id = uuid.uuid4()
create = install(_RecordingCreateScopeJob(job_id=job_id))
rule = make_rule(next_run_at=OCC)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is True
occurrence = session.occurrences[(rule.id, OCC)]
assert occurrence.scrape_job_id == job_id
assert occurrence.fired_at == NOW
print("OK")
"""
        )
    )


def test_an_empty_scope_still_claims_its_occurrence() -> None:
    """FR-015: zero matches -> no job. The occurrence still stands.

    Leaving it unclaimed would let every subsequent pass re-attempt the
    same empty occurrence for as long as the scope stays empty.
    """
    _assert_ok(
        _run(
            """
class _NoMatches(_RecordingCreateScopeJob):
    def __call__(self, session, **kwargs):
        self.calls.append(kwargs)
        return None, None


install(_NoMatches())
rule = make_rule(next_run_at=OCC)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is True
assert session.occurrences[(rule.id, OCC)].scrape_job_id is None
print("OK")
"""
        )
    )


def test_occurrence_key_truncates_to_whole_seconds() -> None:
    """Sub-second drift must not mint a second "distinct" occurrence.

    A microsecond of clock or round-trip difference between two replicas
    would otherwise produce two primary keys for what is plainly one
    occurrence — reintroducing the duplicate the key exists to prevent.
    """
    _assert_ok(
        _run(
            """
install(_RecordingCreateScopeJob())
drifted = OCC.replace(microsecond=837_412)
rule = make_rule(next_run_at=drifted)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is True
assert list(session.occurrences) == [(rule.id, OCC)]
assert scheduler_app.occurrence_key(drifted) == OCC
print("OK")
"""
        )
    )


def test_a_disabled_rule_is_still_refused() -> None:
    """The pre-existing `enabled` predicate survives the new one."""
    _assert_ok(
        _run(
            """
create = install(_RecordingCreateScopeJob())
rule = make_rule(next_run_at=OCC, enabled=False)
session = FakeSession([rule])

assert scheduler_app.fire_refresh_rule(session, rule_id=rule.id, now=NOW) is False
assert create.calls == []
print("OK")
"""
        )
    )


def test_failure_backoff_is_exponential_and_capped_at_six_hours() -> None:
    """`min(2**n x 60s, 6h)` — the durable half of per-rule isolation."""
    _assert_ok(
        _run(
            """
from app.scheduler.refresh import (
    RULE_FAILURE_BACKOFF_MAX_SECONDS,
    failure_backoff_seconds,
)

assert failure_backoff_seconds(1) == 120
assert failure_backoff_seconds(2) == 240
assert failure_backoff_seconds(3) == 480
assert failure_backoff_seconds(8) == 15360
# 2**9 * 60 = 30720 > 21600, so the cap binds from the ninth failure on.
assert failure_backoff_seconds(9) == RULE_FAILURE_BACKOFF_MAX_SECONDS == 21600
assert failure_backoff_seconds(400) == RULE_FAILURE_BACKOFF_MAX_SECONDS
# Monotonic up to the cap, never zero or negative.
previous = 0
for n in range(1, 20):
    value = failure_backoff_seconds(n)
    assert value >= previous > 0 or previous == 0
    assert 0 < value <= RULE_FAILURE_BACKOFF_MAX_SECONDS
    previous = value
print("OK")
"""
        )
    )


def test_a_fault_backs_the_rule_off_and_a_success_clears_the_counter() -> None:
    """`record_rule_failure` / `clear_rule_failures` write the durable half.

    Without this the fair pass's in-process ledger keeps the pass alive
    but leaves the failed rule DUE — so it is reloaded as a candidate on
    the very next poll, burning a fair-share slot every interval on the
    way to its retry bound.
    """
    _assert_ok(
        _run(
            """
from app.scheduler.refresh import clear_rule_failures, record_rule_failure

rule = make_rule(next_run_at=OCC)
session = FakeSession([rule])
factory = lambda: session

new_due = record_rule_failure(
    factory, rule_id=rule.id, error=RuntimeError("boom"), now=NOW
)
assert rule.consecutive_failures == 1
assert new_due == NOW + timedelta(seconds=120) == rule.next_run_at
assert rule.next_run_at > NOW, "a failed rule must leave the due window"
assert rule.last_failure_at == NOW
assert "boom" in rule.last_failure_error

record_rule_failure(factory, rule_id=rule.id, error=RuntimeError("again"), now=NOW)
assert rule.consecutive_failures == 2
assert rule.next_run_at == NOW + timedelta(seconds=240)

clear_rule_failures(factory, rule_id=rule.id)
assert rule.consecutive_failures == 0
assert rule.last_failure_at is None
assert rule.last_failure_error is None
print("OK")
"""
        )
    )


def test_recording_a_failure_never_raises() -> None:
    """A failure while recording a failure must not end the pass."""
    _assert_ok(
        _run(
            """
from app.scheduler.refresh import clear_rule_failures, record_rule_failure


def exploding_factory():
    raise RuntimeError("database is on fire")


assert record_rule_failure(
    exploding_factory, rule_id=uuid.uuid4(), error=RuntimeError("x"), now=NOW
) is None
clear_rule_failures(exploding_factory, rule_id=uuid.uuid4())

# A rule that has since been deleted is not an error either.
empty = FakeSession([])
assert record_rule_failure(
    lambda: empty, rule_id=uuid.uuid4(), error="gone", now=NOW
) is None
print("OK")
"""
        )
    )
