"""``maintenance_cadences`` ORM model — the durable half of the scheduler's
daily maintenance cadences (2026-08-15 readiness cycle, defect
"daily maintenance may never fire").

## Why this table exists at all

`apps/scheduler`'s loop drove every cadence off an in-process float
accumulator that starts at ``0.0`` on each process start and is persisted
nowhere. The three *daily* cadences (`PARTITION_CREATE_INTERVAL_SECONDS`,
`DAILY_ROLLUP_INTERVAL_SECONDS`, `RETENTION_INTERVAL_SECONDS`, all 86400)
therefore only ever fired if the container survived a full 24h without a
restart or redeploy — on Railway, where every deploy restarts the
container, that is not a property the platform offers. The observed
production symptom (2026-08-15) was a partition gap 17 days from becoming
a dated total write outage and a `variant_price_daily_rollups` table that
had never received a single row.

A cadence whose deadline lives in Postgres cannot be reset by a restart:
the countdown is a stored ``next_due_at`` timestamp, not elapsed process
time, so a scheduler that boots overdue runs the task on its first poll
and one that boots early simply waits.

## Shape: global, no RLS

Deliberately **no** ``workspace_id`` and **no** RLS — the same shape as
``domain_playbooks`` / ``proxy_circuit_breakers``. These are
platform-level housekeeping deadlines (partition DDL, cross-tenant
rollups, retention drops), not tenant data; there is no tenant CRUD
surface for them, and every one of the tasks they gate is itself
cross-tenant by construction.

## Concurrency contract

``next_due_at`` is the claim. Any scheduler replica may attempt a claim,
but only the one whose atomic
``UPDATE ... WHERE cadence_key = :k AND next_due_at <= :now RETURNING id``
matches a row actually enqueues the task — the losing replicas re-read a
``next_due_at`` already advanced past ``now`` and match zero rows. This is
the same ``UPDATE ... WHERE ... RETURNING`` lease
``app_shared.access.breaker`` uses for its self-driving evaluator, and the
same duplicate-suppression role ``FOR UPDATE SKIP LOCKED`` plays in the
scheduler's refresh-rule claim. It also makes a restart loop harmless:
the first boot after a due deadline claims it and pushes ``next_due_at``
a full interval into the future, so the next ten boots in the same minute
claim nothing.

See ``app_shared.maintenance.cadence`` for the claim implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime, TimestampMixin

#: Sentinel "never ran" deadline. A brand-new cadence row is born due, so
#: the very first scheduler boot after this migration runs the task
#: immediately rather than waiting a full 24h interval for a countdown
#: that has never been satisfied (this is the "runs promptly on first
#: boot" requirement).
EPOCH_DUE = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Cadence keys. Stable strings — they are the durable identity of a
#: deadline, so renaming one resets that cadence's countdown once.
CADENCE_PARTITION_CREATE = "partition_create"
CADENCE_DAILY_ROLLUP = "daily_rollup"
CADENCE_RETENTION_DROP = "retention_drop"

#: Every cadence the scheduler drives durably (the daily ones). The 60s
#: cadences deliberately stay in-process — see
#: ``app_shared.maintenance.cadence`` module docstring.
DURABLE_CADENCE_KEYS: tuple[str, ...] = (
    CADENCE_PARTITION_CREATE,
    CADENCE_DAILY_ROLLUP,
    CADENCE_RETENTION_DROP,
)


class MaintenanceCadence(Base, TimestampMixin):
    """``maintenance_cadences`` — one durable deadline per ``cadence_key``.

    Global (no ``workspace_id``, no RLS) — see the module docstring.
    """

    __tablename__ = "maintenance_cadences"
    __table_args__ = (
        Index("uq_maintenance_cadences_cadence_key", "cadence_key", unique=True),
    )

    #: Stable identity of the deadline (e.g. ``"partition_create"``).
    cadence_key: Mapped[str] = mapped_column(Text(), nullable=False)
    #: The claim predicate: this cadence is due when ``next_due_at <= now``.
    #: Born at :data:`EPOCH_DUE` so a new row is immediately due.
    next_due_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, default=lambda: EPOCH_DUE
    )
    #: When a claim last succeeded (``NULL`` = never claimed). Operator
    #: visibility + the input to the "cadence has not run in N days"
    #: assertion.
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: Free-form identifier of the process that won the last claim
    #: (hostname/pid) — purely diagnostic for multi-replica deployments.
    last_claimed_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Monotonic count of successful claims, for "is this thing alive".
    run_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)


__all__ = [
    "CADENCE_DAILY_ROLLUP",
    "CADENCE_PARTITION_CREATE",
    "CADENCE_RETENTION_DROP",
    "DURABLE_CADENCE_KEYS",
    "EPOCH_DUE",
    "MaintenanceCadence",
]
