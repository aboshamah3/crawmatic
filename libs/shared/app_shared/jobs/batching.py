"""Pure batch-planning logic (`contracts/batching.md`, D1, FR-011, SC-008).

No DB/Redis/network, no scrapy/twisted/fastapi — unit-testable against
in-memory target/match rows. The dispatch task (`apps/workers/app/
workers/tasks_jobs.py`) resolves each job target's `competitor_domain` +
`mode` set-based (one scoped read over the matches/competitors, never
per-target) and attaches them as :class:`ResolvedTarget` before calling
:func:`plan_batches` — this module never queries anything itself.

A dispatch "batch" is a **derived** grouping, not a persisted row
(research D1): :func:`plan_batches` groups targets by
`(competitor_domain, mode, strategy_method)`, chunks each group to at
most `http_max` match_ids for HTTP-mode groups (the 50-200 guidance
bounds HTTP batches) or `browser_max` for BROWSER-mode groups (M1: a
much smaller 5-25 ceiling, default 15 — a browser run pays a per-target
headless-render cost HTTP does not), and assigns each resulting chunk a
stable `batch_index` — the enumerated position over a **canonical sort**
of the groups so re-planning the exact same targets always yields the
exact same indices.

`batch_index` is a **spider argument and a traceability label, nothing
more** (EPA B1). It used to be half of the dispatch idempotency key
(`dispatched:{scrape_job_id}:{batch_index}`), which was the P0.4A bug:
an index is a *position in a plan*, and the plan is re-derived on every
delivery, so the moment the target set changed shape the same index
named different work and the guard suppressed a dispatch that had never
happened. Idempotency now keys on
:class:`app_shared.scrapyd.identity.DispatchIdentity`, whose work
component is a digest over the batch's actual match_ids. The canonical
sort still matters — it keeps :func:`app_shared.jobs.nodes.select_node`
and the emitted indices stable across a duplicate dispatch — but nothing
correctness-critical rides on it any more.

`strategy_method` joins the grouping key because it is part of that
identity: two targets on the same domain in the same mode but on
different rungs of the strategy chain are different work, and a batch
that mixed them could not be named by a single identity.

`planning_generation` is carried through onto every :class:`Batch`
verbatim from the job's durable strategy-chain state
(`scrape_jobs.planning_generation`). It is an *input*, never derived
here: minting it inside the planner would make every retry look like a
new plan, which is exactly what the durable slot exists to prevent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.enums import ScrapeProfileMode

__all__ = ["Batch", "ResolvedTarget", "plan_batches"]

#: The strategy-method label used when a target has no resolved chain
#: candidate (no strategy profile configured for its domain). A stable,
#: explicit placeholder rather than an empty string so the identity's
#: canonical payload never contains an ambiguous empty field.
DEFAULT_STRATEGY_METHOD = "default"

DEFAULT_HTTP_MIN = 50
DEFAULT_HTTP_MAX = 200
# Browser batches need a much smaller ceiling than HTTP (audit item M1,
# 5-25 guidance range) -- a browser-mode Scrapyd run pays a per-target
# headless-render cost HTTP does not, so the same 200-wide chunk that's
# fine for HTTP would make one browser batch's wall time enormous.
DEFAULT_BROWSER_MAX = 15


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
    #: The versioned strategy-chain rung this target is on (EPA B1), as a
    #: stable label. Defaulted so pre-B1 callers/tests that only know
    #: domain+mode keep working — those simply plan one method's worth of
    #: work, which is what they were already doing implicitly.
    strategy_method: str = DEFAULT_STRATEGY_METHOD


@dataclass(frozen=True)
class Batch:
    """One derived `(domain, mode, strategy_method)` chunk = one Scrapyd run."""

    batch_index: int
    mode: ScrapeProfileMode
    domain: str
    match_ids: list[uuid.UUID]
    strategy_method: str = DEFAULT_STRATEGY_METHOD
    #: The job's durable plan version this batch was planned under —
    #: copied from `plan_batches(planning_generation=...)`, never derived.
    planning_generation: int = 0


def plan_batches(
    targets: list[ResolvedTarget],
    *,
    http_min: int = DEFAULT_HTTP_MIN,
    http_max: int = DEFAULT_HTTP_MAX,
    browser_max: int = DEFAULT_BROWSER_MAX,
    planning_generation: int = 0,
) -> list[Batch]:
    """Group `targets` by `(competitor_domain, mode, strategy_method)`.

    - Every input target lands in exactly one output batch; no match_id
      is duplicated across batches.
    - Each group is chunked into batches of at most `http_max` match_ids
      for HTTP-mode groups, or `browser_max` for BROWSER-mode groups
      (M1: browser batches need a much smaller ceiling than HTTP's 50-200
      guidance); a group smaller than its ceiling forms a single batch —
      no cross-group merging (a batch always carries exactly one domain,
      one mode and one strategy method, so that it can be named by a
      single `DispatchIdentity`).
    - `batch_index` is the stable enumerated position over the groups'
      canonical `(domain, mode, strategy_method)` sort, then chunk order
      within the group — so calling this again on the same input yields
      the same indices (deterministic node selection; and a stable label
      on the spider side). It is NOT an idempotency input — see the
      module docstring.
    - `planning_generation` is stamped verbatim onto every emitted batch.
      It comes from the job's durable strategy-chain state; this function
      neither reads nor advances it.
    - Empty input -> empty list.

    `http_min` is accepted per the contract signature as sizing guidance
    (a group at/above it never needs to split below it); it does not
    trigger merging across distinct groups.
    """
    del http_min  # guidance only — no cross-group merging (see docstring).

    groups: dict[tuple[str, ScrapeProfileMode, str], list[uuid.UUID]] = {}
    for target in targets:
        key = (target.competitor_domain, target.mode, target.strategy_method)
        groups.setdefault(key, []).append(target.match_id)

    batches: list[Batch] = []
    batch_index = 0
    for domain, mode, strategy_method in sorted(
        groups.keys(), key=lambda key: (key[0], key[1], key[2])
    ):
        match_ids = groups[(domain, mode, strategy_method)]
        group_max = browser_max if mode == ScrapeProfileMode.BROWSER else http_max
        for start in range(0, len(match_ids), group_max):
            chunk = match_ids[start : start + group_max]
            batches.append(
                Batch(
                    batch_index=batch_index,
                    mode=mode,
                    domain=domain,
                    match_ids=chunk,
                    strategy_method=strategy_method,
                    planning_generation=planning_generation,
                )
            )
            batch_index += 1

    return batches
