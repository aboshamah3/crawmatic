"""Node selection for one dispatch batch (`contracts/node-selection.md`, D3, FR-014).

Two functions, one pure module — no state, no persistence, no config
reads. The dispatch task picks the mode-appropriate Scrapyd node pool
(`Settings.SCRAPYD_HTTP_URLS` for HTTP batches,
`Settings.SCRAPYD_BROWSER_URLS` for BROWSER batches, I1) and passes it
here.

* :func:`select_node` — the original deterministic hash placement. **Kept
  for the single-node case only** (EPA B6): with one node in the pool
  there is nothing to choose and nothing to probe, so the dispatch path
  short-circuits to it rather than paying for a `daemonstatus.json`
  round trip that cannot change the answer.
* :func:`choose_node` — capacity-aware placement across a pool of two or
  more nodes (EPA B6 / F11). It spreads work by observed queue depth
  (`running + pending`, read by
  :func:`app_shared.jobs.node_load.read_node_loads`), refuses to queue
  more than `max_pending` behind any one node, and returns ``None`` when
  every node is saturated — which the caller must treat as **defer the
  batch, do not POST**, never as "pick someone anyway".

Why `None` rather than "least-bad node": the browser pool runs
``max_proc = 1`` per node, so a batch POSTed onto a full node does not
run sooner — it sits in that node's queue holding a cost-authorization
grant and a `claimed_at` stamp while the phase clock runs. Deferring
leaves the targets offerable to the next pass, which is the cheaper and
more honest outcome.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

__all__ = ["NodeLoad", "UNREACHABLE", "choose_node", "reserve_node", "select_node"]


@dataclass(frozen=True, slots=True)
class NodeLoad:
    """What one Scrapyd node is currently carrying.

    ``running`` and ``pending`` are `daemonstatus.json`'s own counters.
    ``reachable`` is False when the node did not answer at all — which is
    deliberately NOT the same as ``running = pending = 0``: an unanswered
    probe is no evidence of an idle node, so an unreachable node is
    treated exactly like a saturated one and is skipped
    (:func:`choose_node`). It stays that way for as long as the negative
    cache entry lives (60 s, `node_load.UNREACHABLE_TTL_SECONDS`), so one
    dead node cannot be re-probed once per batch.
    """

    running: int = 0
    pending: int = 0
    reachable: bool = True

    @property
    def depth(self) -> int:
        """The number the placement decision orders on: queued + in flight."""
        return self.running + self.pending

    def reserve(self) -> NodeLoad:
        """This load plus one batch we are about to place on it.

        Placement inside a single dispatch pass must see its own
        decisions: four batches choosing against one 10-second-old
        snapshot would otherwise all pick the same "least loaded" node.
        Returns a NEW value — the snapshot the caller holds is never
        mutated behind its back (see :func:`reserve_node`).
        """
        return replace(self, pending=self.pending + 1)


#: The load of a node that did not answer its probe. Saturated by
#: construction (`reachable = False`), whatever `max_pending` is.
UNREACHABLE = NodeLoad(running=0, pending=0, reachable=False)


def _stable_hash(domain: str) -> int:
    """A process-stable digest of `domain` — never Python's salted `hash()`.

    Builtin `hash()` is salted per process via `PYTHONHASHSEED`, so the
    same domain would map to different nodes in different worker
    processes. `blake2b` is deterministic across processes/interpreters
    (FR-014, US3-AS4).
    """
    digest = hashlib.blake2b(domain.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big")


def select_node(domain: str, nodes: list[str]) -> str:
    """Return the node in `nodes` deterministically assigned to `domain`.

    The same `domain` always maps to the same node, in any worker
    process, across dispatch retries — so two retries of one batch can
    never be sent to two different nodes. A single-node pool always
    returns that node.

    **EPA B6 narrowed this function's role to the single-node case.** It
    knows nothing about capacity, so on a multi-node pool it will happily
    keep piling one busy domain onto one node while its neighbours idle;
    :func:`choose_node` is the multi-node entry point. It survives because
    it is still the right answer when the pool has exactly one member, and
    because it supplies :func:`choose_node`'s deterministic tie-break.
    """
    if not nodes:
        raise ValueError("select_node requires a non-empty node pool")
    return nodes[_stable_hash(domain) % len(nodes)]


def choose_node(
    domain: str,
    nodes: list[str],
    *,
    loads: dict[str, NodeLoad],
    max_pending: int,
) -> str | None:
    """The least-loaded node in `nodes` with room for one more batch.

    Ordering, in priority order:

    1. **Eligibility.** A node is eligible only if it answered its probe
       (``load.reachable``) and its ``pending`` is strictly below
       `max_pending`. An unreachable node and a full node are the same
       kind of "not now"; neither is ever picked. A node missing from
       `loads` entirely is treated as unreachable — a caller that failed
       to probe it has no evidence it can take work.
    2. **Least `running + pending`.** The spread rule: batches for one
       domain land on different nodes precisely because placing one
       raises that node's depth (see :func:`reserve_node`).
    3. **Domain affinity as the tie-break.** Among equally loaded nodes
       the pool is rotated so that :func:`select_node`'s deterministic
       choice for `domain` is considered first. On an idle pool this
       reproduces the old hash placement exactly, which keeps one domain
       sticky to one node — and therefore keeps its per-node rate-limit
       and cookie state warm — until load actually diverges.

    Returns:
        The chosen node URL, or ``None`` when **every** node is
        unreachable or at `max_pending`. ``None`` means *defer the
        batch*: the caller must not POST, must not stamp the targets, and
        must leave them offerable to the next pass.

    Raises:
        ValueError: `nodes` is empty (a pool with no members is a
            configuration error, not a capacity condition — collapsing it
            into ``None`` would make a missing `SCRAPYD_*_URLS` look like
            a busy fleet).
    """
    if not nodes:
        raise ValueError("choose_node requires a non-empty node pool")

    # The same index `select_node` would return, computed rather than
    # searched for: a pool that (mis)configures the same URL twice must
    # still rotate to the slot the hash names, not to the first copy.
    preferred = _stable_hash(domain) % len(nodes)
    rotated = nodes[preferred:] + nodes[:preferred]

    best: str | None = None
    best_depth = -1
    for node in rotated:
        load = loads.get(node, UNREACHABLE)
        if not load.reachable or load.pending >= max_pending:
            continue
        if best is None or load.depth < best_depth:
            best = node
            best_depth = load.depth
    return best


def reserve_node(loads: dict[str, NodeLoad], node_url: str) -> dict[str, NodeLoad]:
    """`loads` with one more batch pending on `node_url`.

    Returns a new dict; the caller rebinds it and passes it to the next
    :func:`choose_node` call in the same pass. Without this, a pass that
    places four batches against one snapshot places all four on the same
    node — the snapshot is only refreshed every
    `node_load.DEFAULT_TTL_SECONDS`.
    """
    load = loads.get(node_url, NodeLoad())
    return {**loads, node_url: load.reserve()}
