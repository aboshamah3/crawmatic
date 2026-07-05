"""Discovery-run creation service (`contracts/discovery.md`,
`contracts/api-and-observability.md`) — SPEC-12 US3 T028.

Mirrors `app_shared.jobs.service.create_match_job`'s shape (create rows +
`app_shared.messaging.enqueue`, no Scrapyd import) so the operator
trigger is unit-testable against a fake session + fake `enqueue`, same as
`tests/unit/test_jobs_router.py`. `create_discovery_run` creates the
`PENDING` `strategy_discovery_runs` row synchronously (so the 202
response has something to return) and enqueues the exact same
`STRATEGY_DISCOVERY_RUN` task the automatic trigger (US2
`resolve_or_create_strategy_profile`) uses, passing `run_id` so the
worker task updates this row rather than creating a second one
(`apps/workers/app/workers/tasks_strategy.py::run_discovery`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app_shared.enums import DiscoveryRunStatus
from app_shared.messaging import enqueue
from app_shared.models.strategy import StrategyDiscoveryRun
from app_shared.task_names import STRATEGY_DISCOVERY_RUN

__all__ = ["create_discovery_run"]

#: Matches `apps/workers/app/workers/celery_app.py`'s `strategy_discovery` queue.
_DISCOVERY_QUEUE = "strategy_discovery"


def create_discovery_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    domain: str,
    url_pattern: str,
    sample_urls: list[str],
) -> StrategyDiscoveryRun:
    """Create a `PENDING` run for `(competitor, domain, url_pattern)` and
    enqueue `STRATEGY_DISCOVERY_RUN` (FR-016, FR-019 already enforced by
    the caller's Pydantic schema validation before this is ever called).
    """
    run = StrategyDiscoveryRun(
        workspace_id=workspace_id,
        competitor_id=competitor_id,
        domain=domain,
        url_pattern=url_pattern,
        sample_size=len(sample_urls),
        status=DiscoveryRunStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    enqueue(
        STRATEGY_DISCOVERY_RUN,
        queue=_DISCOVERY_QUEUE,
        kwargs={
            "workspace_id": str(workspace_id),
            "competitor_id": str(competitor_id),
            "domain": domain,
            "url_pattern": url_pattern,
            "sample_urls": list(sample_urls),
            "triggered_by": "OPERATOR",
            "run_id": str(run.id),
        },
    )
    return run
