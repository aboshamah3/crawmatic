"""Cancel a finished job's still-live Scrapyd runs where they live (2026-09-29, E3.3).

**The failure.** Nothing removed a job's queued runs from a Scrapyd node once
the job itself was over. ``cancel.json`` was reachable only from the admin
cancel path, and even there it defaulted to ``SCRAPYD_HTTP_URLS[0]`` -- the
browser node was never asked. After the nightly job hit its 12 h deadline
the browser node still held 2,725 pending runs; each would later spawn a
Chromium process only for ``load_targets`` to find every target terminal and
exit, one slot at a time, while real work queued behind them.

**What this does.** Every dispatch intent records the node it was POSTed to
(``node_url``) and the project half of its ``node_class``. For one job, this
asks each (node, project) once for its run states and cancels exactly the
runs still ``pending`` or ``running`` there. Finished runs are left alone;
an unreachable node is counted and skipped -- cancelling is a cost
optimisation, never a correctness requirement (a late run of a finished job
changes nothing: its targets are terminal and the fence refuses cancelled
work).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app_shared.models.dispatch import DispatchIntent
from app_shared.repository import scoped_select

logger = logging.getLogger(__name__)

__all__ = ["PurgeReport", "purge_live_runs"]

_LIVE_RUN_STATES = frozenset({"pending", "running"})


@dataclass(frozen=True)
class PurgeReport:
    cancelled: int = 0
    unreachable_nodes: int = 0
    failed_cancels: int = 0


def purge_live_runs(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    client: Any,
) -> PurgeReport:
    """Cancel ``scrape_job_id``'s pending/running runs on the nodes that hold them.

    ``client`` needs ``list_job_states(node_url, project)`` and
    ``cancel(jobid, node_url=..., project=...)`` -- the
    :class:`~app_shared.scrapyd.client.ScrapydDispatchClient` surface.
    Never raises for a node problem; the report says what happened.
    """
    job_uuid = scrape_job_id if isinstance(scrape_job_id, uuid.UUID) else uuid.UUID(str(scrape_job_id))
    intents = (
        session.execute(
            scoped_select(DispatchIntent, workspace_id).where(
                DispatchIntent.scrape_job_id == job_uuid,
                DispatchIntent.node_url != "",
            )
        )
        .scalars()
        .all()
    )
    by_node: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for intent in intents:
        project = intent.node_class.split(":", 1)[0] if intent.node_class else None
        by_node[(intent.node_url, project)].append(str(intent.scrapyd_job_id))

    cancelled = unreachable = failed = 0
    for (node_url, project), job_ids in by_node.items():
        try:
            states = client.list_job_states(node_url, project)
        except Exception as exc:  # noqa: BLE001 - best effort by contract
            unreachable += 1
            logger.warning(
                "run_purge.listjobs_unavailable scrape_job_id=%s node=%s project=%s (%s)",
                job_uuid,
                node_url,
                project,
                exc,
            )
            continue
        for job_id in job_ids:
            if states.get(job_id) not in _LIVE_RUN_STATES:
                continue
            try:
                if client.cancel(job_id, node_url=node_url, project=project):
                    cancelled += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001 - best effort by contract
                failed += 1
                logger.warning(
                    "run_purge.cancel_failed scrape_job_id=%s node=%s jobid=%s",
                    job_uuid,
                    node_url,
                    job_id,
                    exc_info=True,
                )
    if cancelled or unreachable or failed:
        logger.info(
            "run_purge scrape_job_id=%s cancelled=%d unreachable_nodes=%d failed=%d",
            job_uuid,
            cancelled,
            unreachable,
            failed,
        )
    return PurgeReport(cancelled=cancelled, unreachable_nodes=unreachable, failed_cancels=failed)
