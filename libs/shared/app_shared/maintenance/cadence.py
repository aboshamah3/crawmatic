"""Durable maintenance cadences (2026-08-15 readiness cycle).

## The defect this replaces

`apps/scheduler`'s loop kept every cadence in an in-process float
accumulator::

    partition_create_elapsed = 0.0
    ...
    if partition_create_elapsed >= partition_create_interval:

Those floats start at ``0.0`` on every process start and are persisted
nowhere. `PARTITION_CREATE_INTERVAL_SECONDS`,
`DAILY_ROLLUP_INTERVAL_SECONDS` and `RETENTION_INTERVAL_SECONDS` all
default to ``86400``, so on a platform that restarts containers (Railway:
every deploy, every OOM, every host migration) the 24h countdown could be
reset indefinitely and the daily maintenance tasks *never* fire. That is
not hypothetical: production on 2026-08-15 had no ``2026_09`` partition on
any of the four partitioned tables (a dated, total write outage) and zero
rows ever written to ``variant_price_daily_rollups``.

## The fix

Store the **deadline**, not the elapsed time. ``maintenance_cadences``
holds one row per cadence with a ``next_due_at`` timestamp; the scheduler
asks the database "is this due?" instead of asking its own uptime. A
restart is then invisible to the cadence: the deadline is where it was.

* **Prompt on first boot / when overdue** — a never-run cadence is born
  with ``next_due_at`` at :data:`~app_shared.models.maintenance_cadence.EPOCH_DUE`,
  so it is due immediately; an overdue one is due immediately. Neither
  waits an interval.
* **Multi-replica safe** — the claim is a single atomic
  ``UPDATE ... WHERE cadence_key = :k AND next_due_at <= :now RETURNING id``.
  Row-level locking serialises concurrent updaters; the first to commit
  advances ``next_due_at``, and every other replica's identical statement
  then matches zero rows. Same ``UPDATE ... RETURNING`` lease shape as
  ``app_shared.access.breaker.evaluate_and_persist``, same
  one-claimant-wins role that ``FOR UPDATE SKIP LOCKED`` plays in
  ``app.scheduler.refresh.run_refresh_pass``.
* **No thundering herd on restart loops** — because a winning claim
  pushes ``next_due_at`` a full interval forward *before* the task is
  enqueued, a scheduler that crash-loops ten times in a minute enqueues
  the task once, not ten times.

## Why the 60s cadences stay in-process

Deliberate. The refresh poll (30s), strategy flush / finalize / recover /
redispatch (60s) and the outbox drain (5s) / reconcile (300s) are all
short enough that a restart costs at most one interval — seconds, not a
month — and all of them are idempotent sweeps whose *work* is durably
recorded elsewhere (rules in ``refresh_rules.next_run_at``, messages in
``outbox_messages``). Making them durable would add one write per
scheduler tick per cadence — up to ~12 extra transactions/minute of pure
bookkeeping — to protect against a loss that is already bounded by
seconds. The failure mode that made the daily cadences catastrophic (the
interval being of the same order as the restart period) simply does not
apply to them.

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.models.maintenance_cadence import (
    DURABLE_CADENCE_KEYS,
    EPOCH_DUE,
    MaintenanceCadence,
)

logger = logging.getLogger("maintenance.cadence")

#: ERROR-level structured log event emitted when a durable cadence has not
#: successfully claimed for longer than its own overdue tolerance. See
#: `contracts/observability.md` conventions (`event key=value` pairs).
EVENT_CADENCE_OVERDUE = "maintenance_cadence_overdue"


def claimant_id() -> str:
    """Best-effort identifier of this process, for `last_claimed_by`.

    Purely diagnostic (which replica won the claim); never a correctness
    input, so a hostname lookup failure degrades to the pid alone rather
    than raising into the scheduler loop.
    """
    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001 - diagnostic only
        host = "unknown"
    return f"{host}/{os.getpid()}"


def ensure_cadence_rows(session: Session, keys: tuple[str, ...] = DURABLE_CADENCE_KEYS) -> None:
    """Create any missing cadence rows, born due (idempotent).

    The migration seeds these, but a cadence key added later (or a
    database restored from before the seed) must not silently disable its
    task — so the scheduler self-heals the rows on boot. ``ON CONFLICT DO
    NOTHING`` on the unique ``cadence_key`` index makes this safe against
    a concurrent replica doing exactly the same thing.
    """
    for key in keys:
        session.execute(
            text(
                """
                INSERT INTO maintenance_cadences
                    (id, cadence_key, next_due_at, run_count, created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :cadence_key, :epoch, 0, now(), now())
                ON CONFLICT (cadence_key) DO NOTHING
                """
            ).bindparams(cadence_key=key, epoch=EPOCH_DUE)
        )


def claim_cadence(
    session: Session,
    cadence_key: str,
    *,
    interval_seconds: int,
    now: datetime | None = None,
    claimed_by: str | None = None,
) -> bool:
    """Atomically claim ``cadence_key`` if it is due; return whether we won.

    ``True`` means *this* caller is the one that must run the task now —
    ``next_due_at`` has already been advanced to ``now + interval_seconds``
    and ``last_run_at``/``run_count`` stamped. ``False`` means the cadence
    is not yet due, or another replica claimed it first.

    The caller commits: the advance and whatever the caller does with the
    claim land in one transaction, so a caller that fails before
    committing releases the deadline rather than silently consuming it.

    Note the deadline is computed from ``now`` and not from the previous
    ``next_due_at``: a cadence that was overdue by three weeks fires once
    and then resumes a normal daily rhythm, instead of firing 21 times to
    "catch up" on work that is idempotent anyway.
    """
    now = now or datetime.now(timezone.utc)
    claimed = session.execute(
        text(
            """
            UPDATE maintenance_cadences
               SET next_due_at = :next_due_at,
                   last_run_at = :now,
                   last_claimed_by = :claimed_by,
                   run_count = run_count + 1,
                   updated_at = :now
             WHERE cadence_key = :cadence_key
               AND next_due_at <= :now
            RETURNING id
            """
        ).bindparams(
            cadence_key=cadence_key,
            now=now,
            next_due_at=now + timedelta(seconds=interval_seconds),
            claimed_by=claimed_by or claimant_id(),
        )
    ).first()
    return claimed is not None


@dataclass(frozen=True)
class CadenceStatus:
    """One cadence's durable state, for the overdue assertion."""

    cadence_key: str
    next_due_at: datetime
    last_run_at: datetime | None
    run_count: int

    def overdue_seconds(self, now: datetime) -> float:
        """Seconds past this cadence's deadline (``0.0`` when not due)."""
        return max(0.0, (now - self.next_due_at).total_seconds())


def read_cadence_statuses(
    session: Session, keys: tuple[str, ...] = DURABLE_CADENCE_KEYS
) -> list[CadenceStatus]:
    """Read the durable state of every named cadence (missing rows omitted)."""
    rows = (
        session.query(MaintenanceCadence)  # noqa: workspace-scope - global housekeeping table, no tenant data
        .filter(MaintenanceCadence.cadence_key.in_(keys))
        .order_by(MaintenanceCadence.cadence_key)
        .all()
    )
    return [
        CadenceStatus(
            cadence_key=row.cadence_key,
            next_due_at=row.next_due_at,
            last_run_at=row.last_run_at,
            run_count=row.run_count,
        )
        for row in rows
    ]


def log_overdue_cadences(
    statuses: list[CadenceStatus],
    *,
    now: datetime,
    grace_seconds: int,
) -> list[str]:
    """Emit :data:`EVENT_CADENCE_OVERDUE` at ERROR for each stuck cadence.

    "Stuck" means the deadline passed more than ``grace_seconds`` ago —
    i.e. the scheduler is up but is not claiming, which is a different
    failure from the task running and failing downstream (that one is
    caught by the outcome assertions in
    ``app_shared.maintenance.health``). Returns the offending keys so a
    caller can act on them.
    """
    overdue: list[str] = []
    for status in statuses:
        seconds = status.overdue_seconds(now)
        if seconds <= grace_seconds:
            continue
        overdue.append(status.cadence_key)
        logger.error(
            "%s cadence_key=%s overdue_seconds=%d last_run_at=%s run_count=%d",
            EVENT_CADENCE_OVERDUE,
            status.cadence_key,
            int(seconds),
            status.last_run_at.isoformat() if status.last_run_at else "never",
            status.run_count,
        )
    return overdue


__all__ = [
    "EVENT_CADENCE_OVERDUE",
    "CadenceStatus",
    "claim_cadence",
    "claimant_id",
    "ensure_cadence_rows",
    "log_overdue_cadences",
    "read_cadence_statuses",
]
