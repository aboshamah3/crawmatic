"""Cached `daemonstatus.json` reads for capacity-aware placement (EPA B6 / F11).

:func:`read_node_loads` turns a Scrapyd node pool into the
``dict[str, NodeLoad]`` :func:`app_shared.jobs.nodes.choose_node` needs.
It is a thin, deliberately dumb layer over
:meth:`app_shared.scrapyd.client.ScrapydDispatchClient.daemon_status`
with one job beyond the HTTP call: **make the probe cheap enough to run
on every dispatch pass.**

Two caches, two TTLs, one Redis key per node:

* a node that answered is cached for ``ttl`` seconds (default 10). Ten
  seconds is short enough that a node draining its queue is noticed
  within one dispatch pass, and long enough that a burst of batches for
  one job probes each node once rather than once per batch.
* a node that did NOT answer is cached for
  :data:`UNREACHABLE_TTL_SECONDS` (60), as :func:`choose_node`'s contract
  requires: an unreachable node is treated as saturated *for 60 s*. The
  longer negative TTL is the point — a dead node must not be re-probed
  (and re-timed-out, at `_DEFAULT_TIMEOUT_SECONDS` a go) by every pass
  for a minute.

Redis is a cache here and nothing more. Every read and write is
defensive: a Redis outage degrades this to "probe every node every pass",
which is slower but still correct, and must never be able to stop
dispatch. The one thing it may not do is invent capacity — a failed
*probe* is `NodeLoad(reachable=False)`, never `NodeLoad()`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app_shared.jobs.nodes import UNREACHABLE, NodeLoad, choose_node, reserve_node, select_node

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "UNREACHABLE_TTL_SECONDS",
    "NodePlacement",
    "node_load_key",
    "read_node_loads",
]

logger = logging.getLogger(__name__)

#: How long a node's *answered* status is reused.
DEFAULT_TTL_SECONDS = 10
#: How long a node that did not answer stays classified saturated. Named
#: by B6's acceptance criteria ("treated as saturated for 60 s").
UNREACHABLE_TTL_SECONDS = 60

_KEY_PREFIX = "scrapyd:nodeload:"


class _RedisLike(Protocol):
    """The two Redis calls this module makes (mirrors `scrapyd/client.py`)."""

    def set(  # noqa: D102 - protocol stub
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> bool | None: ...

    def get(self, name: str) -> str | None: ...  # noqa: D102 - protocol stub


def node_load_key(node_url: str) -> str:
    """The cache key for one node. One key per node, shared fleet-wide.

    Deliberately NOT workspace-scoped: a node's queue depth is a property
    of the node, not of the tenant asking, and every worker process in
    the fleet benefits from one probe. It holds no tenant data — two
    integers and a boolean.
    """
    return f"{_KEY_PREFIX}{node_url.rstrip('/')}"


def _as_int(value: object) -> int | None:
    """`int(value)` or ``None`` — never raises on a malformed payload.

    Same rule as `tasks_jobs._queue_depth`: a node that put ``null`` or a
    list in its `pending` field has not told us it is idle, so the
    payload is discarded rather than coerced to zero.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _decode(raw: str | bytes | None) -> NodeLoad | None:
    """A cached entry back into a :class:`NodeLoad`, or ``None`` if unusable."""
    if raw is None:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if not payload.get("reachable", True):
            return UNREACHABLE
        running = _as_int(payload.get("running"))
        pending = _as_int(payload.get("pending"))
        if running is None or pending is None:
            return None
        return NodeLoad(running=running, pending=pending, reachable=True)
    except Exception:  # pragma: no cover - defensive; a bad cache entry is a miss
        return None


def _encode(load: NodeLoad) -> str:
    return json.dumps(
        {
            "running": load.running,
            "pending": load.pending,
            "reachable": load.reachable,
        }
    )


def _probe(client: Any, node_url: str) -> NodeLoad:
    """One `daemonstatus.json` read, classified.

    ``daemon_status`` never raises — it returns ``None`` on any
    transport/HTTP/parse error — so the only failure mode left here is a
    payload that answered but whose counters are not numbers. That is
    treated as unreachable too: a node whose status we cannot read is a
    node we cannot size.
    """
    payload = client.daemon_status(node_url)
    if not isinstance(payload, dict):
        return UNREACHABLE
    running = _as_int(payload.get("running", 0))
    pending = _as_int(payload.get("pending", 0))
    if running is None or pending is None:
        return UNREACHABLE
    return NodeLoad(running=running, pending=pending, reachable=True)


def read_node_loads(
    client: Any,
    nodes: list[str],
    redis: _RedisLike | None,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> dict[str, NodeLoad]:
    """Current load for every node in `nodes`, cached in Redis.

    Args:
        client: anything exposing ``daemon_status(node_url) -> dict | None``
            (:class:`~app_shared.scrapyd.client.ScrapydDispatchClient` in
            production, a fake node in tests).
        nodes: the pool to size. Duplicates are probed once.
        redis: the cache. ``None`` (or a Redis that errors) simply means
            every node is probed on every call.
        ttl: seconds an *answered* status is reused for. An unanswered
            one always uses :data:`UNREACHABLE_TTL_SECONDS`, regardless
            of `ttl` — the negative cache is a correctness property of
            B6's placement rule, not a tuning knob.

    Returns:
        One entry per distinct node. Every node in `nodes` is present, so
        :func:`choose_node` never has to distinguish "absent" from
        "unreachable".
    """
    loads: dict[str, NodeLoad] = {}
    for node_url in nodes:
        if node_url in loads:
            continue
        key = node_load_key(node_url)
        cached = None
        if redis is not None:
            try:
                cached = _decode(redis.get(key))
            except Exception:
                logger.warning(
                    "node_load: cache read failed for node=%s -- probing", node_url
                )
                cached = None
        if cached is not None:
            loads[node_url] = cached
            continue

        load = _probe(client, node_url)
        loads[node_url] = load
        if redis is not None:
            expiry = ttl if load.reachable else UNREACHABLE_TTL_SECONDS
            try:
                redis.set(key, _encode(load), ex=max(1, int(expiry)))
            except Exception:
                logger.warning(
                    "node_load: cache write failed for node=%s -- continuing", node_url
                )
    return loads


class NodePlacement:
    """Capacity-aware node placement for the span of ONE dispatch pass (B6/F11).

    Holds the two things :func:`~app_shared.jobs.nodes.choose_node` is
    pure enough not to: the `daemonstatus.json` snapshot for each pool
    (read once per pass through `load_reader`, itself Redis-cached
    fleet-wide for `DEFAULT_TTL_SECONDS`), and the batches this pass has
    already placed but not yet POSTed
    (:func:`~app_shared.jobs.nodes.reserve_node`) — without which four
    batches decided against one 10-second-old snapshot would all pick the
    same "least loaded" node.

    Three rules, in order:

    1. **An identity that already has an intent row keeps its node.** A
       retry reads `intent.node_url` (EPA B2) and never re-chooses:
       `reconcile_inflight_intents` asks exactly ONE node whether the run
       exists, so a retry that moved the batch would be a run nobody can
       find and a re-POST nobody can suppress.
    2. **A single-node pool short-circuits to
       :func:`~app_shared.jobs.nodes.select_node`.** There is nothing to
       choose and a probe cannot change the answer, so it is not paid for
       — which is also every deployment's situation today
       (`docs/ops/CAPACITY.md`).
    3. **Otherwise `choose_node`**, which may answer ``None``: every node
       unreachable or at `max_pending`. ``None`` means DEFER — the caller
       must not authorize, POST or stamp.
    """

    def __init__(
        self,
        *,
        max_pending: int,
        load_reader: Any,
        unreachable_pool_fallback: bool = False,
    ) -> None:
        self._max_pending = int(max_pending)
        #: What to do when NOT ONE node in the pool answered its probe.
        #:
        #: ``False`` (the dispatch path): defer, like any other "no room"
        #: answer. Nothing has started, so leaving the targets `PENDING`
        #: for the next pass costs nothing and POSTs nothing at a fleet
        #: that is not there.
        #:
        #: ``True`` (the stall-recovery path): fall back to
        #: :func:`~app_shared.jobs.nodes.select_node` and re-POST anyway.
        #: A totally unreachable pool is not a capacity condition, and it
        #: is the *precondition of the reaper's whole job*: a target is
        #: only stalled-and-recoverable because the node holding it went
        #: away (F-2, `tasks_jobs.recover_stalled_batches`). Deferring
        #: there would mean a dead pool permanently suppresses the one
        #: sweep that exists to move work off dead nodes. The re-POST
        #: costs nothing if the pool really is gone -- it fails, and the
        #: failure path releases the grant immediately -- and it recovers
        #: the work the moment one node comes back. A pool with a
        #: reachable-but-FULL node still defers here: that one is a
        #: genuine capacity answer.
        self._unreachable_pool_fallback = bool(unreachable_pool_fallback)
        #: ``(nodes) -> dict[str, NodeLoad]``. Injected rather than built
        #: here so the caller owns *when* a Scrapyd client (and its Redis
        #: connection) comes into existence — a single-node deployment
        #: never creates one at all.
        self._load_reader = load_reader
        self._loads: dict[tuple[str, ...], dict[str, NodeLoad]] = {}
        #: Batches this pass deferred, for the caller's summary log.
        self.deferred = 0

    def place(
        self,
        *,
        domain: str,
        nodes: list[str],
        intents: Any = None,
        identity: Any = None,
    ) -> str | None:
        """The node this batch goes to, or ``None`` to defer it."""
        if intents is not None and identity is not None:
            already = intents.planned_node_url(identity)
            if already:
                return str(already)
        if len(nodes) == 1:
            return select_node(domain, nodes)
        key = tuple(nodes)
        loads = self._loads.get(key)
        if loads is None:
            loads = self._load_reader(list(nodes))
            self._loads[key] = loads
        node_url = choose_node(
            domain, list(nodes), loads=loads, max_pending=self._max_pending
        )
        if node_url is None:
            if self._unreachable_pool_fallback and not any(
                load.reachable for load in loads.values()
            ):
                return select_node(domain, list(nodes))
            self.deferred += 1
            return None
        self._loads[key] = reserve_node(loads, node_url)
        return node_url
