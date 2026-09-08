"""Scheduler refresh pass (SPEC-13 US2/US3, `contracts/scheduler-loop.md`).

`run_refresh_pass(session_factory, *, now, batch_limit) -> int` is the
custom DB-driven enqueuer invoked on a poll interval from
`scheduler_app.py`. Scraping-free (no scrapy/twisted/playwright/fastapi
import anywhere in this module — Principle I / FR-019).

Each due `RefreshRule` is claimed, processed, and committed in **its own
transaction** — never a single batch transaction (research R5). This is
what lets enqueue-before-commit (FR-012) and per-rule error isolation
(FR-021, added in US3/T024) coexist: a per-rule `SAVEPOINT` rollback
cannot un-send an already-enqueued Celery dispatch, which would orphan
it against a rolled-back `scrape_job_id`.

The claim uses `SELECT ... FOR UPDATE SKIP LOCKED` on the BYPASSRLS
system session (`app_shared.database.get_system_session`) — the due-rule
scan is inherently cross-tenant, so the claim query is a **sanctioned
unscoped** `RefreshRule` access (annotated `# noqa: workspace-scope`,
the same pattern as the pre-auth `User`/`ApiKey` lookups in
`apps/api/app/deps.py`/`app_shared.security.status_cache`) — it must see
due rows across every workspace in one query. Workspace isolation for
every job/target read/write within a claimed rule's transaction is
preserved at the application layer by `create_scope_job`
(`scoped_select(..., rule.workspace_id)` + explicit `workspace_id=` on
every insert).

No global/advisory pass-lock (FR-009) — `SKIP LOCKED` alone guarantees
each due rule is claimed by at most one instance/transaction at a time
(US3 AS-1, SC-003). Priority is deliberately **not** in `ORDER BY`
(advisory only, §28 / autospec-decisions).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeJobSource, ScrapeJobType, ScrapeScope
from app_shared.jobs.service import create_scope_job
from app_shared.models.refresh_rules import RefreshRule
from app_shared.scheduling.cadence import compute_next_run_at

logger = logging.getLogger("scheduler.refresh")

#: Characters of a failure repr kept on `refresh_rules.last_failure_error`
#: -- a breadcrumb, never a blob (the same bound
#: `app_shared.scheduling.fair_queue.RetryLedger` applies to its own
#: in-process copy of the message).
_RULE_ERROR_CHARS = 500

__all__ = [
    "RULE_FAILURE_BACKOFF_BASE_SECONDS",
    "RULE_FAILURE_BACKOFF_MAX_SECONDS",
    "clear_rule_failures",
    "failure_backoff_seconds",
    "record_rule_failure",
    "run_refresh_pass",
]


def _target_id_for_rule(rule: RefreshRule):
    """Return the non-null scope-target id for ``rule.scope``, or ``None`` for WORKSPACE."""
    if rule.scope is ScrapeScope.WORKSPACE:
        return None
    if rule.scope is ScrapeScope.COMPETITOR:
        return rule.competitor_id
    if rule.scope is ScrapeScope.PRODUCT:
        return rule.product_id
    if rule.scope is ScrapeScope.VARIANT:
        return rule.product_variant_id
    if rule.scope is ScrapeScope.PRODUCT_GROUP:
        return rule.product_group_id
    if rule.scope is ScrapeScope.MATCH:
        return rule.match_id
    raise ValueError(f"unsupported scope {rule.scope!r}")


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: Base of the per-rule failure backoff, in seconds (`2**n * base`).
RULE_FAILURE_BACKOFF_BASE_SECONDS = 60
#: Ceiling of the per-rule failure backoff: 6 hours. Past this a rule is
#: not "backing off" any more, it is broken — and the bounded-retry
#: ledger has already dead-lettered it long before the cap binds.
RULE_FAILURE_BACKOFF_MAX_SECONDS = 6 * 60 * 60


def failure_backoff_seconds(consecutive_failures: int) -> int:
    """``min(2**n * 60s, 6h)`` for ``n`` consecutive failures (n >= 1)."""
    n = max(1, int(consecutive_failures))
    if n >= 32:  # 2**32 * 60 overflows nothing here but is pointless to compute
        return RULE_FAILURE_BACKOFF_MAX_SECONDS
    return min(
        (2**n) * RULE_FAILURE_BACKOFF_BASE_SECONDS,
        RULE_FAILURE_BACKOFF_MAX_SECONDS,
    )


def record_rule_failure(
    session_factory: Callable[[], Session],
    *,
    rule_id: uuid.UUID | str,
    error: BaseException | str,
    now: datetime,
) -> datetime | None:
    """Make one rule's failure DURABLE and back it off. Never raises.

    The other half of per-rule isolation (EPA B3/F07). The in-process
    :class:`~app_shared.scheduling.fair_queue.RetryLedger` already decides
    *retry vs dead letter* and already keeps the pass running past a
    failure — but its counter dies with the process, and, more
    importantly, a failed rule whose ``next_run_at`` is untouched is
    STILL DUE. It is re-loaded as a candidate on the very next poll, fails
    again, and burns a fair-share slot every interval until it exhausts
    its retry bound.

    So the failure is written where it survives: ``consecutive_failures``
    is incremented on the row and ``next_run_at`` is pushed out
    ``min(2**n x 60s, 6h)``. The pass CONTINUES either way — this
    function's exceptions are swallowed, because a failure while recording
    a failure must not become the thing that stops the pass.

    Returns the new ``next_run_at``, or ``None`` if nothing was written.
    """
    message = (error if isinstance(error, str) else repr(error))[:_RULE_ERROR_CHARS]
    try:
        with session_factory() as session:
            rule = (
                session.execute(
                    select(RefreshRule)  # noqa: workspace-scope
                    .where(RefreshRule.id == _as_uuid(rule_id))
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .first()
            )
            if rule is None:
                session.rollback()
                return None
            failures = int(rule.consecutive_failures or 0) + 1
            backoff = failure_backoff_seconds(failures)
            next_run_at = now + timedelta(seconds=backoff)
            rule.consecutive_failures = failures
            rule.last_failure_at = now
            rule.last_failure_error = message
            rule.next_run_at = next_run_at
            session.commit()
            logger.warning(
                "scheduler: rule %s failed (consecutive=%d), backing off %ds to %s",
                rule_id,
                failures,
                backoff,
                next_run_at.isoformat(),
            )
            return next_run_at
    except Exception:
        logger.exception(
            "scheduler: could not record the failure of rule %s — the pass "
            "continues, but this rule stays due and will be retried",
            rule_id,
        )
        return None


def clear_rule_failures(
    session_factory: Callable[[], Session], *, rule_id: uuid.UUID | str
) -> None:
    """Reset ``consecutive_failures`` after a success. Never raises.

    Mirrors :meth:`RetryLedger.record_success` on the durable side, and
    for the same reason: a rule that fails, succeeds, and fails again is
    flaky, not poison, and flaky work must not accumulate its way to a
    six-hour backoff over weeks. A no-op for the (overwhelmingly common)
    rule whose counter is already zero, so a clean fleet pays one indexed
    primary-key read per firing and no write.
    """
    try:
        with session_factory() as session:
            rule = (
                session.execute(
                    select(RefreshRule)  # noqa: workspace-scope
                    .where(RefreshRule.id == _as_uuid(rule_id))
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .first()
            )
            if rule is None or not rule.consecutive_failures:
                session.rollback()
                return
            rule.consecutive_failures = 0
            rule.last_failure_at = None
            rule.last_failure_error = None
            session.commit()
    except Exception:
        logger.exception("scheduler: could not clear failures for rule %s", rule_id)


def run_refresh_pass(
    session_factory: Callable[[], Session],
    *,
    now: datetime,
    batch_limit: int,
) -> int:
    """Claim and fire up to ``batch_limit`` due ``refresh_rules``, per-rule.

    ``session_factory`` is called once per iteration (e.g.
    ``app_shared.database.get_system_sessionmaker()`` — a plain
    SQLAlchemy ``sessionmaker``, itself callable to yield a fresh
    ``Session`` that is its own context manager) so every claimed rule
    gets its **own** transaction: claim one row with ``FOR UPDATE SKIP
    LOCKED`` -> resolve its scope to ACTIVE matches and create+enqueue a
    `SCHEDULED`/`SCHEDULER` job via `create_scope_job` (empty scope -> no
    job, no dispatch, FR-015) -> advance
    ``last_run_at``/``locked_at``/``next_run_at`` -> commit. Returns the
    number of rules fired.

    Loop stops when: ``batch_limit`` rules have been fired, ``batch_limit``
    rules have FAILED (EPA B3/F07 — see below), or no more due rows remain
    (``SELECT ... LIMIT 1`` returns nothing — every due row is either
    fired by this pass or held by a concurrent claimant/instance).

    The failure bound is new and is what lets a failure ``continue``
    rather than ``break``. A failed rule is backed off out of the due
    window by `record_rule_failure`, so it cannot be re-selected and the
    loop cannot spin — but a fleet where EVERY due rule fails would
    otherwise walk the entire backlog in one pass. Bounding failures by
    the same number that bounds successes keeps one pass's cost bounded
    whatever the mix, and the next tick continues from where this one
    stopped.

    **This is the fallback path.** ``SCHEDULER_FAIR_QUEUE_ENABLED``
    defaults to ``True`` since B3, so `run_fair_scheduling_pass` is what
    normally runs; this loop is what an operator falls back to. It gets
    the same per-rule isolation and the same durable backoff, but not the
    fair pass's retry ledger, dead letter or two-plane caps.
    """
    fired = 0
    failed = 0
    while fired < batch_limit and failed < batch_limit:
        with session_factory() as session:
            rule = (
                session.execute(
                    select(RefreshRule)  # noqa: workspace-scope
                    .where(RefreshRule.enabled, RefreshRule.next_run_at <= now)
                    .order_by(RefreshRule.next_run_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if rule is None:
                session.rollback()
                break

            try:
                run_time = now
                target_id = _target_id_for_rule(rule)
                create_scope_job(
                    session,
                    workspace_id=rule.workspace_id,
                    scope=rule.scope,
                    target_id=target_id,
                    requested_by=None,
                    job_type=ScrapeJobType.SCHEDULED,
                    source=ScrapeJobSource.SCHEDULER,
                )

                rule.last_run_at = run_time
                rule.locked_at = run_time
                rule.next_run_at = compute_next_run_at(rule, run_time)

                session.commit()  # enqueue already happened; commit last
                fired += 1
            except Exception as exc:  # noqa: BLE001 - isolated per rule
                # FR-021: this is the SAME rollback/leave-fields-unchanged
                # path used for the crash-before-commit case (FR-014) --
                # only THIS rule's transaction is undone. Its SKIP LOCKED
                # row lock releases, so nothing this rule half-did
                # survives. Any dispatch that already reached the broker
                # is neutralized by the SPEC-08 idempotent dispatch guard
                # + SPEC-11 match locks (duplicate-over-miss).
                rule_id = rule.id
                session.rollback()
                logger.exception("refresh rule %s failed", rule_id)
                failed += 1
                # EPA B3/F07: this used to `break`, and the comment that
                # justified it was right about the mechanism -- with
                # next_run_at unchanged the poison rule is re-selected by
                # the identical claim query and the pass spins -- but
                # wrong about the remedy. Ending the pass means ONE bad
                # rule stops every other tenant's scheduling for a full
                # poll interval, which is the failure this task exists to
                # remove.
                #
                # The right fix is to make the rule not-due instead of
                # making the pass stop: `record_rule_failure` increments
                # `consecutive_failures` and pushes `next_run_at` out
                # `min(2**n x 60s, 6h)` in its OWN transaction (so the
                # rolled-back one cannot take the backoff with it). The
                # rule is then no longer selected by the claim query, the
                # loop continues to the next due rule, and a rule that is
                # merely flaky comes back on its own once the backoff
                # elapses. Identical accounting to the fair pass's, which
                # is the path that carries the retry ledger and the dead
                # letter on top of it.
                record_rule_failure(
                    session_factory, rule_id=rule_id, error=exc, now=now
                )
                continue

    return fired
