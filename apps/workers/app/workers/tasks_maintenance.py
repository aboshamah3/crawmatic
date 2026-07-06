"""`maintenance` queue tasks — SPEC-15 retention/rollups/partition upkeep.

US1 (P1 MVP, contracts/partition-creation.md): ``partition_create`` keeps
current + next month's partitions in place for every *existing*
registered table (`app_shared.maintenance.registry.PARTITIONED_TABLES`),
self-healing and idempotent, so the calendar never causes a write outage
(SC-001). US2's ``daily_rollup`` and US3's ``retention_drop`` tasks land
in later phases of this same module.

All three maintenance tasks run on the BYPASSRLS system session
(`app_shared.database.get_system_session`) — the sanctioned SPEC-13
cross-tenant seam (research R9): partition ``CREATE``/``DROP`` DDL and
the rollup/retention cross-tenant source scans need an elevated role
under `FORCE ROW LEVEL SECURITY`. App-level workspace scoping is
preserved wherever a rollup row is actually read/written (not applicable
to this phase — `create_missing_partitions` touches no workspace rows,
only DDL + catalog reads).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.database import get_system_session
from app_shared.maintenance.partitions import create_missing_partitions
from app_shared.task_names import MAINTENANCE_PARTITION_CREATE

logger = logging.getLogger("workers.maintenance")


@app.task(name=MAINTENANCE_PARTITION_CREATE)
def partition_create() -> None:
    """`MAINTENANCE_PARTITION_CREATE` (`maintenance` queue,
    contracts/partition-creation.md, FR-004/005/006/007/008).

    Opens a BYPASSRLS system session, calls
    `create_missing_partitions` for the current time and
    `Settings.PARTITION_CREATE_LOOKAHEAD_MONTHS` months of lookahead,
    commits, and emits one structured run-report log line (FR-023) —
    `tables_skipped_absent` (e.g. `webhook_events` until SPEC-16, FR-002)
    and `partitions_created` (empty on a no-op re-run, FR-006).
    """
    settings = get_settings()
    with get_system_session() as session:
        report = create_missing_partitions(
            session,
            now_utc=datetime.now(timezone.utc),
            lookahead_months=settings.PARTITION_CREATE_LOOKAHEAD_MONTHS,
        )
        session.commit()

    logger.info(
        "maintenance_partition_create tables_skipped_absent=%s partitions_created=%s",
        report.tables_skipped_absent,
        report.partitions_created,
    )
