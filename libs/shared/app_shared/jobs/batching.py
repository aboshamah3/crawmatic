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
    #: EPA W4.3 (single-workspace same-URL coalescing, report §8): the
    #: ledger's own canonical URL grouping key
    #: (`app_shared.netledger.recorder.canonical_url_hash`), attached by
    #: the caller — this module never computes or re-implements
    #: canonicalization, and never reads this field itself.
    #: `plan_batches`'s grouping key stays exactly `(competitor_domain,
    #: mode, strategy_method)`; this field exists only so
    #: `app_shared.jobs.coalescing.cluster_for_coalescing` can reorder
    #: targets (behind `JOBS_COALESCING_ENABLED`) before they reach
    #: `plan_batches`, so a same-URL cluster lands in one chunk instead of
    #: splitting across a chunk boundary by accident of input order.
    #: `None` for every pre-W4.3 construction site (including every
    #: existing unit test) — a target with no attached identity is never
    #: coalesced with anything (see `coalescing.cluster_for_coalescing`).
    canonical_url_hash: str | None = None
    # -- EPA C4 (F08): the rest of the coalescing equivalence key --------
    #
    # W4.3 keyed coalescing on `canonical_url_hash` alone, which is only
    # *half* an identity: two matches can share a canonical URL and still
    # be different physical fetches — a different proxy exit country gets
    # a different storefront, a different transport gets a different
    # response body, a different variant selector reads a different offer
    # off the same page. Folding those onto one fetch would fan a WRONG
    # price out to the siblings. Every field below therefore SPLITS
    # clusters (never merges them), and every one defaults to `None` so a
    # caller that attaches none of them gets exactly the pre-C4 grouping.
    #
    # `app_shared.jobs.coalescing.coalescing_key` is the one place these
    # are composed; nothing reads them individually.
    #: Owning workspace. Present so the key is COMPLETE and can be
    #: asserted on, not because this module ever spans workspaces — every
    #: caller resolves targets for one `workspace_id` at a time. See
    #: `coalescing.coalescing_key` for what happens when a caller mixes
    #: two (it refuses).
    workspace_id: uuid.UUID | None = None
    #: Market/region the price is being read for (e.g. a storefront
    #: locale). Different region, different offer.
    region: str | None = None
    #: Currency the price is expected in. A same-URL fetch that resolves
    #: to a different currency is a different result, not a duplicate.
    currency: str | None = None
    #: Proxy exit country for this fetch (`request_attempts.proxy_country`).
    #: The single most consequential splitter: `SA` and `AE` exits of the
    #: same URL routinely return different prices and availability.
    proxy_country: str | None = None
    #: Transport actually used (`AccessMethod` value). Distinct from
    #: `strategy_method`, which is the ladder RUNG's label: two rungs can
    #: share a transport, and the same rung can be re-planned onto
    #: another, so neither substitutes for the other in the key.
    transport: str | None = None
    #: Stable hash of the variant-selector config that will be applied to
    #: the fetched page. Two matches on one multi-variant page are one
    #: fetch only when they read the SAME variant.
    variant_selector_hash: str | None = None
    #: EPA C6 (F18): the domain playbook's ``cheap_path`` (an
    #: :class:`~app_shared.enums.AccessMethod` value), i.e. the rung the
    #: ladder starts on for this domain. NOT part of the coalescing key —
    #: it is a property of the DOMAIN, identical for every target in a
    #: group, and it says nothing about whether two fetches return the
    #: same bytes. It rides here only because :func:`plan_batches` is the
    #: one place that can carry a per-domain fact onto the derived
    #: :class:`Batch` without a second read. ``None`` = no playbook, which
    #: means "no hint", exactly as it does for the ladder.
    cheap_path: str | None = None


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
    # -- EPA C6 (F18): what this batch will actually COST -----------------
    #
    # Before F18 the dispatch site derived its cost-authorization
    # reservation from `mode` alone and authorized every HTTP batch as
    # PROXY "fail-closed", because the planner could not know which rung
    # the spider would land on. It can: the ladder already picked the rung
    # (`ResolvedTarget.transport`), and the playbook already named the
    # cheap one (`ResolvedTarget.cheap_path`). Both are stamped here so
    # `tasks_jobs._batch_authorization_request` reserves for the rung that
    # will actually be tried instead of over-reserving a paid ceiling
    # against traffic that never touches a provider.
    #
    # All three fields default to `None`, and every consumer falls back to
    # the pre-F18 mode-derived behaviour when they are unset — a caller
    # that attaches nothing (every pre-C6 construction site, including
    # every existing test) plans exactly as it did before.
    #: The :class:`~app_shared.enums.AccessMethod` value of the rung this
    #: batch will attempt FIRST. `None` when the group's targets disagree
    #: or none carried one — "we do not know" must never be mistaken for
    #: "we know it is free".
    initial_transport: str | None = None
    #: The domain playbook's `cheap_path` for this batch's domain, carried
    #: verbatim from `ResolvedTarget.cheap_path`. `initial_transport`
    #: differing from this is what makes a dispatch an ESCALATION rather
    #: than a first attempt.
    cheap_transport: str | None = None
    #: Count of DISTINCT physical fetches this batch will make — the
    #: coalesced count, not `len(match_ids)`. Three matches pointing at one
    #: canonical URL under one equivalence key are one fetch, and reserving
    #: three requests for them books a request ceiling against work that
    #: will never happen. `None` when no target carried a coalescing
    #: identity, in which case a consumer falls back to `len(match_ids)`.
    unique_physical_requests: int | None = None


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

    groups: dict[tuple[str, ScrapeProfileMode, str], list[ResolvedTarget]] = {}
    for target in targets:
        key = (target.competitor_domain, target.mode, target.strategy_method)
        groups.setdefault(key, []).append(target)

    batches: list[Batch] = []
    batch_index = 0
    for domain, mode, strategy_method in sorted(
        groups.keys(), key=lambda key: (key[0], key[1], key[2])
    ):
        members = groups[(domain, mode, strategy_method)]
        group_max = browser_max if mode == ScrapeProfileMode.BROWSER else http_max
        for start in range(0, len(members), group_max):
            chunk = members[start : start + group_max]
            batches.append(
                Batch(
                    batch_index=batch_index,
                    mode=mode,
                    domain=domain,
                    match_ids=[target.match_id for target in chunk],
                    strategy_method=strategy_method,
                    planning_generation=planning_generation,
                    initial_transport=_unanimous(chunk, "transport"),
                    cheap_transport=_unanimous(chunk, "cheap_path"),
                    unique_physical_requests=_unique_physical_requests(chunk),
                )
            )
            batch_index += 1

    return batches


def _unanimous(chunk: list[ResolvedTarget], field: str) -> str | None:
    """`chunk`'s single agreed value for `field`, or `None` if it disagrees.

    A chunk always shares one `(domain, mode, strategy_method)`, so in
    practice its members agree — but the grouping key does not *contain*
    either field, so agreement is a property of the caller, not of this
    function. Disagreement therefore degrades to `None` ("we do not
    know"), which every consumer treats as the fail-closed pre-F18
    behaviour. Returning one member's value and hoping would be a guess
    about money.
    """
    values = {getattr(target, field, None) for target in chunk}
    values.discard(None)
    if len(values) != 1:
        return None
    return str(values.pop())


def _unique_physical_requests(chunk: list[ResolvedTarget]) -> int | None:
    """How many DISTINCT physical fetches `chunk` will make (EPA C6/F18).

    Uses `app_shared.jobs.coalescing.coalescing_key` — the ONE equivalence
    key in the codebase — so this count can never disagree with what
    `cluster_for_coalescing` actually folds together. A target with no
    `canonical_url_hash` gets a synthetic per-match key there, so it is
    counted on its own; if NO target in the chunk carries an identity this
    returns `None` rather than `len(chunk)`, because "nobody attached an
    identity" is not evidence that every fetch is distinct — it is the
    absence of evidence, and the consumer's `len(match_ids)` fallback is
    the conservative reading.

    Imported lazily: `coalescing` imports this module at module scope, so
    a module-level import here would be a cycle.
    """
    from app_shared.jobs.coalescing import coalescing_key

    if not any(target.canonical_url_hash is not None for target in chunk):
        return None
    return len({coalescing_key(target) for target in chunk})
