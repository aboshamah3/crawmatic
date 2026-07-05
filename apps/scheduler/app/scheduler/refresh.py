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
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeJobSource, ScrapeJobType, ScrapeScope
from app_shared.jobs.service import create_scope_job
from app_shared.models.refresh_rules import RefreshRule
from app_shared.scheduling.cadence import compute_next_run_at

logger = logging.getLogger("scheduler.refresh")

__all__ = ["run_refresh_pass"]


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

    Loop stops when: ``batch_limit`` rules have been fired, or no more
    due rows remain (``SELECT ... LIMIT 1`` returns nothing — every due
    row is either fired by this pass or held by a concurrent
    claimant/instance).
    """
    fired = 0
    while fired < batch_limit:
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

            session.commit()
            fired += 1

    return fired
