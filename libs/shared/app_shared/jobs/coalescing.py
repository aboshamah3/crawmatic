"""Single-workspace same-canonical-URL coalescing (EPA W4.3, plan §8).

**Scope, per the plan**: Stage 1 only — SINGLE-workspace coalescing of
identical-canonical-URL matches within one dispatch. Cross-workspace
coalescing is a separate, later, owner-gated decision; nothing here
builds toward it, and nothing here *can* reach across workspaces: every
caller in this codebase resolves :class:`~app_shared.jobs.batching.
ResolvedTarget`\\ s for exactly one ``workspace_id`` at a time
(``tasks_jobs._resolve_domains_and_modes`` takes a single
``workspace_id``, never a set of them) — the boundary is structural, not
a filter this module has to enforce.

What this module builds: **concurrent-batch coalescing**
------------------------------------------------------------
Prior art already exists at the spider layer: the 2026-08-11
``SCRAPE_URL_DEDUP`` fix (the scraping runtime's own targets module,
``group_targets_for_dedup``) folds same-job targets that resolve to a
byte-identical fetch onto one fetcher, fanning the result out to every
sibling match — the "one
physical operation, five logical attempts, five ``request_attempts``
rows, one allocation at fraction 1.0" case C1/C4 were built for. That
mechanism is real and shipped, but it only ever sees the match_ids
inside ONE Scrapyd run: :func:`app_shared.jobs.batching.plan_batches`
chunks each ``(domain, mode, strategy_method)`` group at ``http_max``/
``browser_max`` (50-200 / 5-25 targets) in *whatever order the resolved
targets arrived in* — so two matches sharing a canonical URL can land in
different chunks purely by accident of ordering, and dispatch them to
two different spider runs where the in-run dedup can never see them
both.

:func:`cluster_for_coalescing` closes that gap at the ONE layer this
task is scoped to touch (job planning, not the spider): it stable-sorts
resolved targets so every target sharing a ``canonical_url_hash`` is
contiguous *before* the list reaches :func:`~app_shared.jobs.batching.
plan_batches`, so chunking keeps a same-URL cluster in the same chunk
(and therefore the same spider run) whenever the cluster is smaller than
the chunk ceiling — the ordinary case. A cluster that itself exceeds the
ceiling still splits (an over-200-duplicate cluster is not the case this
stage optimizes for); that is a known, documented limit, not a
correctness bug — a split cluster simply dispatches as two separate
physical fetches, exactly like today.

Feature flag: ``JOBS_COALESCING_ENABLED`` (default ``False``,
``app_shared.config.Settings``). OFF means :func:`cluster_for_coalescing`
is never called and planning is byte-identical to pre-W4.3 (the OFF
default is pinned by ``tests/unit/test_w4_flag_defaults.py``; the
byte-identity of unreordered planning by the pre-existing, unmodified
``tests/unit/test_jobs_batching.py``). ON reorders the input
to ``plan_batches`` only — it never changes ``plan_batches`` itself, the
grouping key, the chunk ceilings, or ``batch.match_ids`` cardinality (a
coalesced cluster still carries every one of its match_ids in the batch;
only the DOWNSTREAM spider-level fetch count drops, and only once
``SCRAPE_URL_DEDUP`` is *also* on — the two flags are independent and
BOTH must be enabled for a fetch to actually collapse. This module does
not, and per its file-scope, cannot flip ``SCRAPE_URL_DEDUP``).

The canonical identity
-----------------------
:func:`~app_shared.netledger.recorder.canonical_url_hash` is never
re-implemented here — this module does not compute the hash at all, it
reads the ``ResolvedTarget.canonical_url_hash`` its callers already
stamped with that exact function (``apps/workers``'
``tasks_jobs``/``tasks_dispatch`` import it directly). The module
docstring on that function is explicit
that the ledger's ``network_operations.canonical_url_hash`` grouping key
must be the SAME function every caller uses, or "one operation, five
attempts" silently becomes "five operations that all happen to look
alike". A target with no canonical identity attached (``None`` — every
existing/legacy ``ResolvedTarget`` construction site that predates W4.3)
is never coalesced with anything, including another ``None`` target —
"we don't know this target's identity" must never collapse into "these
two targets share an identity" by coincidence of both being unset.

What this module explicitly does NOT build: cache reuse
---------------------------------------------------------
The plan distinguishes two mechanisms: concurrent-batch coalescing
(above — one fetch dispatched for many matches at once) and cache reuse
(a recent-enough COMPLETED fetch of the same canonical URL is reused
with NO new fetch at all, even across separate dispatch passes). Cache
reuse needs a live DB read against ``network_operations`` (fleet-owned,
no ``workspace_id``) joined through ``network_operation_allocations``
(the RLS-protected, workspace-owned half) at dispatch time, plus a write
path that marks a target satisfied without ever calling the spider — a
materially larger, materially riskier change than reordering a list,
and not made natural by the planning code as it stands today. Per the
plan's own scope note ("implement both only if the planning code makes
both natural; otherwise implement concurrent-batch coalescing ... and
document the cache-reuse variant as future work"), this module builds
ONLY concurrent-batch coalescing. :func:`is_within_freshness_window` is
the pure freshness predicate a future cache-reuse pass would need
(paired with ``JOBS_COALESCING_FRESHNESS_SECONDS``) — included now,
proven correct by unit tests and by the W4.3 canary's scripted
simulation, but not wired into any live dispatch path.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from app_shared.jobs.batching import ResolvedTarget

__all__ = [
    "cluster_for_coalescing",
    "coalesced_groups",
    "is_within_freshness_window",
]


def cluster_for_coalescing(targets: Sequence[ResolvedTarget]) -> list[ResolvedTarget]:
    """Stable-reorder `targets` so same-`canonical_url_hash` targets are adjacent.

    Pure, no I/O. A **stable** sort: targets that don't share a hash keep
    their original relative order, so the only thing this can change
    about the eventual ``plan_batches`` output (for a fixed set of
    ceilings) is which chunk a same-URL cluster's members land in
    relative to OTHER matches in the same ``(domain, mode,
    strategy_method)`` group — never which group they belong to, never
    ``batch_index`` determinism (that is a separate canonical sort over
    the GROUP keys, untouched here), and never the total match_id count.

    Targets carrying no hash (``canonical_url_hash is None`` — every
    caller that hasn't opted in) sort after every hashed target, among
    themselves in original order: a caller that never attaches hashes at
    all gets its input order back completely unchanged for every group,
    which is exactly today's behaviour and is what makes the flag-OFF
    path byte-identical without a separate code branch.
    """
    return sorted(
        targets,
        key=lambda target: (
            target.canonical_url_hash is None,
            target.canonical_url_hash or "",
        ),
    )


def coalesced_groups(
    targets: Sequence[ResolvedTarget],
) -> dict[str, list[uuid.UUID]]:
    """Diagnostic/test helper: `match_id`s grouped by shared canonical identity.

    Shows what :func:`cluster_for_coalescing` brings together (subject to
    the chunk-ceiling caveat in the module docstring) — not itself part
    of the planning path. A target with no hash gets its own singleton
    group keyed by its match_id, so "no identity" can never be mistaken
    for "shares an identity with another no-identity target".
    """
    groups: dict[str, list[uuid.UUID]] = {}
    for target in targets:
        key = (
            target.canonical_url_hash
            if target.canonical_url_hash is not None
            else f"__no_identity__:{target.match_id}"
        )
        groups.setdefault(key, []).append(target.match_id)
    return groups


def is_within_freshness_window(
    reference: datetime, now: datetime, freshness_seconds: int
) -> bool:
    """Is `reference` (an operation's `closed_at`) still fresh at `now`?

    Pure predicate for the freshness window a future cache-reuse pass
    would gate on (``JOBS_COALESCING_FRESHNESS_SECONDS``) — see the
    module docstring for why cache reuse itself is not wired up yet.
    Exercised directly by the W4.3 canary's scripted simulation against
    the scratch DB restore to prove the semantics the plan calls for:
    "stale window forces a fresh fetch".

    ``freshness_seconds <= 0`` means "never reusable" (a conservative,
    explicit off-switch shape, not an accidental always-true from a
    negative window). A `reference` in the future (clock skew, a bad
    caller) is treated as NOT fresh rather than fresh — reuse must never
    be granted on a fact that has not happened yet.
    """
    if freshness_seconds <= 0:
        return False
    age_seconds = (now - reference).total_seconds()
    return 0 <= age_seconds <= freshness_seconds


def urls_by_match_id(
    resolved: Sequence[ResolvedTarget],
) -> Mapping[uuid.UUID, str | None]:
    """`{match_id: canonical_url_hash}` — a read-only view for tests/canaries.

    Not used by the planning path itself (`cluster_for_coalescing` reads
    the field off each `ResolvedTarget` directly); provided so a test or
    the canary script can assert on the identity a given match resolved
    to without reaching into `ResolvedTarget` internals.
    """
    return {target.match_id: target.canonical_url_hash for target in resolved}
