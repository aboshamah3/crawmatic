"""Pure batch-planning logic (`contracts/batching.md`, D1, FR-011, SC-008).

No DB/Redis/network, no scrapy/twisted/fastapi — unit-testable against
in-memory target/match rows. The dispatch task (`apps/workers/app/
workers/tasks_jobs.py`) resolves each job target's `competitor_domain` +
`mode` set-based (one scoped read over the matches/competitors, never
per-target) and attaches them as :class:`ResolvedTarget` before calling
:func:`plan_batches` — this module never queries anything itself.

A dispatch "batch" is a **derived** grouping, not a persisted row
(research D1): :func:`plan_batches` groups targets by
`(competitor_domain, mode)`, chunks each group to at most `http_max`
match_ids (the 50-200 guidance bounds HTTP batches), and assigns each
resulting chunk a stable `batch_index` — the enumerated position over a
**canonical sort** of the groups (`(domain, mode)`) so re-planning the
exact same targets always yields the exact same indices. That
determinism is what lets the dispatch task's idempotency guard
(`dispatched:{scrape_job_id}:{batch_index}`) and :func:`app_shared.jobs.
nodes.select_node` behave correctly across a duplicate/retried dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.enums import ScrapeProfileMode

__all__ = ["Batch", "ResolvedTarget", "plan_batches"]

DEFAULT_HTTP_MIN = 50
DEFAULT_HTTP_MAX = 200


@dataclass(frozen=True)
class ResolvedTarget:
    """One job target plus its resolved competitor domain + scrape mode.

    Attached by the caller (the dispatch task resolves domain/mode
    set-based from the matches/competitors, not per-target) —
    :func:`plan_batches` receives these already resolved; it never
    queries.
    """

    match_id: uuid.UUID
    competitor_domain: str
    mode: ScrapeProfileMode


@dataclass(frozen=True)
class Batch:
    """One derived `(domain, mode)` chunk to schedule as a single Scrapyd run."""

    batch_index: int
    mode: ScrapeProfileMode
    domain: str
    match_ids: list[uuid.UUID]


def plan_batches(
    targets: list[ResolvedTarget],
    *,
    http_min: int = DEFAULT_HTTP_MIN,
    http_max: int = DEFAULT_HTTP_MAX,
) -> list[Batch]:
    """Group `targets` by `(competitor_domain, mode)` into HTTP batches.

    - Every input target lands in exactly one output batch; no match_id
      is duplicated across batches.
    - Each `(domain, mode)` group is chunked into batches of at most
      `http_max` match_ids; a group smaller than `http_max` (down to the
      `http_min` guidance) forms a single batch — no cross-group merging
      (a batch always carries exactly one domain + one mode).
    - `batch_index` is the stable enumerated position over the groups'
      canonical `(domain, mode)` sort, then chunk order within the
      group — so calling this again on the same input yields the same
      indices (supports dispatch idempotency + deterministic node
      selection).
    - Empty input -> empty list.

    `http_min` is accepted per the contract signature as sizing guidance
    (a group at/above it never needs to split below it); it does not
    trigger merging across distinct `(domain, mode)` groups, since a
    `Batch` always carries exactly one domain and one mode.
    """
    del http_min  # guidance only — no cross-group merging (see docstring).

    groups: dict[tuple[str, ScrapeProfileMode], list[uuid.UUID]] = {}
    for target in targets:
        key = (target.competitor_domain, target.mode)
        groups.setdefault(key, []).append(target.match_id)

    batches: list[Batch] = []
    batch_index = 0
    for domain, mode in sorted(groups.keys(), key=lambda key: (key[0], key[1])):
        match_ids = groups[(domain, mode)]
        for start in range(0, len(match_ids), http_max):
            chunk = match_ids[start : start + http_max]
            batches.append(
                Batch(batch_index=batch_index, mode=mode, domain=domain, match_ids=chunk)
            )
            batch_index += 1

    return batches
