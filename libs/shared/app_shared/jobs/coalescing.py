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

Feature flag: ``JOBS_COALESCING_ENABLED`` (default ``True`` since EPA
plan task B4, 2026-09-04, "H3" -- shipped ``False`` for the W4.3 canary
period; ``app_shared.config.Settings``). OFF means
:func:`cluster_for_coalescing` is never called and planning is
byte-identical to pre-W4.3 (that OFF path stays reachable and pinned by
``tests/unit/test_w4_flag_defaults.py``, which now asserts the ON
default and that the environment can still override it back to
``False``; the byte-identity of unreordered planning itself is pinned by
the pre-existing, unmodified ``tests/unit/test_jobs_batching.py``). ON reorders the input
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

The equivalence key (EPA C4, plan §11 item 5)
----------------------------------------------
W4.3 keyed coalescing on ``canonical_url_hash`` alone. That is half an
identity: two matches can share a canonical URL and still be different
physical fetches. C4 widens it to :class:`CoalescingKey` —
``(workspace_id, canonical_url_hash, region, currency, proxy_country,
transport, variant_selector_hash)``, composed once in
:func:`coalescing_key` — because each of the added components changes
what comes back over the wire: a different proxy exit country gets a
different storefront's prices, a different transport gets a different
response body, a different variant selector reads a different offer off
the same page. Folding those onto one fetch would fan a *wrong* price
out to the siblings, which is strictly worse than paying twice.

Every added component can only SPLIT clusters relative to W4.3's
URL-only key; none of them can merge two that were separate. And each
one defaults to ``None`` on :class:`~app_shared.jobs.batching.
ResolvedTarget`, so a caller that attaches none of them gets exactly the
pre-C4 grouping — this widening cannot regress a caller that has not
adopted it.

**Cross-workspace sharing stays OFF.** ``workspace_id`` is in the key so
the key is complete and assertable, not as a step toward sharing:
:func:`coalesced_groups` *raises*
:class:`~app_shared.costauth.service.CrossWorkspaceCoalescingUnsupported`
on a mixed-workspace input rather than grouping across it, matching the
contract the cost-authorization service already enforces for the same
reason (a fetch shared between tenants has no answer to whose budget
paid for it). The audit's "up to 50 % of fetches are duplicates" upper
bound came from a duplicate-heavy pilot data set and is not evidence for
lifting that.

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

from app_shared.costauth.service import CrossWorkspaceCoalescingUnsupported
from app_shared.jobs.batching import ResolvedTarget

__all__ = [
    "CoalescingKey",
    "CrossWorkspaceCoalescingUnsupported",
    "cluster_for_coalescing",
    "coalesced_groups",
    "coalescing_key",
    "is_within_freshness_window",
]


class CoalescingKey(tuple):
    """The equivalence key two targets must SHARE to become one fetch.

    ``(workspace_id, canonical_url_hash, region, currency, proxy_country,
    transport, variant_selector_hash)`` — EPA C4, plan §11 item 5.

    A ``tuple`` subclass rather than a dataclass so it sorts, hashes and
    compares with zero ceremony (this type is used as a dict key and as a
    sort key on every planning pass) while still having a name a reader
    can look up. Field order is the declared order above and is part of
    the contract: :func:`cluster_for_coalescing` sorts on it, so changing
    the order changes which targets end up adjacent.

    Every component is nullable *except* the identity itself: a key whose
    ``canonical_url_hash`` is ``None`` is never equal to any other key,
    including another ``None`` one — see :func:`coalescing_key`.
    """

    __slots__ = ()

    _FIELDS = (
        "workspace_id",
        "canonical_url_hash",
        "region",
        "currency",
        "proxy_country",
        "transport",
        "variant_selector_hash",
    )

    @property
    def workspace_id(self) -> str | None:
        return self[0]

    @property
    def canonical_url_hash(self) -> str | None:
        return self[1]

    @property
    def has_identity(self) -> bool:
        """Whether this key names a fetch that MAY be shared at all."""
        return not str(self[1]).startswith(_NO_IDENTITY_PREFIX)


#: Prefix of the synthetic, per-target identity given to a target that
#: carries no ``canonical_url_hash``. "We do not know this target's
#: identity" must never collapse into "these two targets share an
#: identity" by coincidence of both being unset (W4.3's rule, kept).
_NO_IDENTITY_PREFIX = "__no_identity__:"


def coalescing_key(target: ResolvedTarget) -> CoalescingKey:
    """The full equivalence key for one resolved target (EPA C4).

    Pure. Two targets are candidates for ONE physical fetch iff this
    returns equal keys for both — which requires the same workspace, the
    same canonical URL, the same region/currency, the same proxy exit
    country, the same transport and the same variant selector. Any one of
    those differing means the two fetches would not have returned the
    same bytes, so folding them would fan a wrong price out to the
    sibling. Every added component can only SPLIT clusters relative to
    W4.3's URL-only key; none of them can merge two that were separate.

    A target with no ``canonical_url_hash`` gets a synthetic key unique to
    its ``match_id``, so it is never coalesced with anything (including
    another identity-less target).
    """
    identity = target.canonical_url_hash
    if identity is None:
        identity = f"{_NO_IDENTITY_PREFIX}{target.match_id}"
    workspace = target.workspace_id
    return CoalescingKey(
        (
            None if workspace is None else str(workspace),
            identity,
            target.region,
            target.currency,
            target.proxy_country,
            target.transport,
            target.variant_selector_hash,
        )
    )


def _sort_key(target: ResolvedTarget) -> tuple:
    """Total order over :func:`coalescing_key`, ``None``s last and stable.

    ``sorted`` cannot compare ``None`` with ``str``, and the key is full
    of optional components, so each one is widened to
    ``(is_none, value_or_empty)``. Identity-less targets keep sorting
    after every identified target, exactly as in W4.3.
    """
    key = coalescing_key(target)
    return (not key.has_identity,) + tuple(
        (component is None, component or "") for component in key
    )


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
    return sorted(targets, key=_sort_key)


def coalesced_groups(
    targets: Sequence[ResolvedTarget],
) -> dict[CoalescingKey, list[uuid.UUID]]:
    """Diagnostic/test helper: `match_id`s grouped by :func:`coalescing_key`.

    Shows what :func:`cluster_for_coalescing` brings together (subject to
    the chunk-ceiling caveat in the module docstring) — not itself part
    of the planning path. A target with no canonical hash gets its own
    singleton group, so "no identity" can never be mistaken for "shares
    an identity with another no-identity target".

    **Cross-workspace sharing stays OFF.** If the input mixes two
    workspaces this raises
    :class:`~app_shared.costauth.service.CrossWorkspaceCoalescingUnsupported`
    rather than silently producing groups that span them: the audit's
    "up to 50 % duplicate" figure is a duplicate-heavy pilot artefact and
    is not a reason to share a paid fetch — and a shared fetch across
    tenants has no answer to "whose budget paid for it", which is exactly
    what the cost-authorization service refuses for the same reason. The
    check is a guard, not a filter: no caller in this codebase can reach
    it (each resolves targets for one `workspace_id` at a time), so
    tripping it means a NEW caller got the boundary wrong.
    """
    workspaces = {
        target.workspace_id for target in targets if target.workspace_id is not None
    }
    if len(workspaces) > 1:
        raise CrossWorkspaceCoalescingUnsupported(
            "coalescing spans "
            f"{len(workspaces)} workspaces; single-workspace coalescing only "
            "(cross-workspace sharing is a separate, owner-gated decision "
            "and has no answer to which workspace's budget paid for the fetch)"
        )
    groups: dict[CoalescingKey, list[uuid.UUID]] = {}
    for target in targets:
        groups.setdefault(coalescing_key(target), []).append(target.match_id)
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
