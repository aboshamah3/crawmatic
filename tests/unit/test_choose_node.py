"""Capacity-aware node placement unit tests (EPA B6 / F11).

`app_shared.jobs.nodes.choose_node` — still pure (no state, no config,
no I/O), but now ordering on observed queue depth rather than on a hash
alone. The five properties B6 exists to guarantee:

1. batches for ONE domain spread across a four-node pool by least
   `running + pending`;
2. a node already at `max_pending` is skipped;
3. all nodes saturated -> `None`, which the dispatch path must read as
   "defer the batch", never as "pick someone anyway";
4. a retry of an EXISTING dispatch intent never calls `choose_node` at
   all — it reads `intent.node_url` (`_NodePlacement.place`);
5. an unreachable node (`daemon_status` -> `None`) counts as saturated,
   and stays that way for 60 s (the negative cache TTL — asserted here
   on the constant + `read_node_loads`'s behaviour, and end-to-end in
   `test_node_load_cache.py`).
"""

from __future__ import annotations

from typing import Any

import pytest

from app_shared.jobs.node_load import (
    UNREACHABLE_TTL_SECONDS,
    NodePlacement,
    read_node_loads,
)
from app_shared.jobs.nodes import (
    UNREACHABLE,
    NodeLoad,
    choose_node,
    reserve_node,
    select_node,
)

_NODES = [
    "http://scrapers-browser-1:6800",
    "http://scrapers-browser-2:6800",
    "http://scrapers-browser-3:6800",
    "http://scrapers-browser-4:6800",
]

_MAX_PENDING = 4


def _idle() -> dict[str, NodeLoad]:
    return {node: NodeLoad() for node in _NODES}


def _place(count: int, *, domain: str = "amazon.sa", loads=None) -> list[str]:
    """Place `count` batches for one domain, as a dispatch pass does."""
    current = _idle() if loads is None else dict(loads)
    placed: list[str] = []
    for _ in range(count):
        node = choose_node(domain, _NODES, loads=current, max_pending=_MAX_PENDING)
        placed.append(node)
        if node is not None:
            current = reserve_node(current, node)
    return placed


# --- 1. spread ---------------------------------------------------------------


def test_four_nodes_spread_one_domains_batches_by_least_running_plus_pending() -> None:
    placed = _place(8)

    assert set(placed) == set(_NODES), "every node in the pool took work"
    assert sorted(placed.count(node) for node in _NODES) == [2, 2, 2, 2]
    # ...and never twice in a row while an idler exists.
    assert all(a != b for a, b in zip(placed, placed[1:]))


def test_placement_orders_on_running_plus_pending_not_pending_alone() -> None:
    loads = {
        _NODES[0]: NodeLoad(running=1, pending=3),  # depth 4
        _NODES[1]: NodeLoad(running=1, pending=1),  # depth 2
        _NODES[2]: NodeLoad(running=3, pending=0),  # depth 3
        _NODES[3]: NodeLoad(running=0, pending=1),  # depth 1  <- least
    }

    assert (
        choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING)
        == _NODES[3]
    )


def test_an_idle_pool_still_honours_the_deterministic_domain_affinity() -> None:
    """Tie-break = `select_node`, so an idle pool reproduces D3/FR-014."""
    for domain in ("amazon.sa", "noon.com", "extra.com", "jarir.com"):
        assert choose_node(
            domain, _NODES, loads=_idle(), max_pending=_MAX_PENDING
        ) == select_node(domain, _NODES)


# --- 2. bounded queues -------------------------------------------------------


def test_a_node_at_max_pending_is_skipped() -> None:
    loads = _idle()
    loads[select_node("amazon.sa", _NODES)] = NodeLoad(running=0, pending=_MAX_PENDING)
    full = select_node("amazon.sa", _NODES)

    chosen = choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING)

    assert chosen is not None
    assert chosen != full


def test_max_pending_bounds_pending_only_a_busy_node_is_still_eligible() -> None:
    """`running` is work in flight, not queue depth — it must not exclude."""
    loads = {node: NodeLoad(running=9, pending=0) for node in _NODES}

    assert (
        choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING)
        is not None
    )


def test_a_pass_cannot_queue_more_than_max_pending_per_node() -> None:
    placed = _place(_MAX_PENDING * len(_NODES) + 3)

    assert placed.count(None) == 3, "the overflow is deferred, not squeezed in"
    for node in _NODES:
        assert placed.count(node) == _MAX_PENDING


# --- 3. all saturated -> None ------------------------------------------------


def test_all_nodes_saturated_returns_none() -> None:
    loads = {node: NodeLoad(running=1, pending=_MAX_PENDING) for node in _NODES}

    assert choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING) is None


def test_an_empty_pool_raises_rather_than_deferring() -> None:
    """A missing `SCRAPYD_*_URLS` is a misconfiguration, not a busy fleet."""
    with pytest.raises(ValueError):
        choose_node("amazon.sa", [], loads={}, max_pending=_MAX_PENDING)


def test_a_node_absent_from_loads_is_treated_as_unreachable() -> None:
    """A caller that failed to probe a node has no evidence it can take work."""
    assert choose_node("amazon.sa", _NODES, loads={}, max_pending=_MAX_PENDING) is None


# --- 4. retries read `intent.node_url`, they never re-choose ------------------


class _RecordingIntents:
    """The half of `DispatchIntentStore` placement touches."""

    def __init__(self, node_url: str | None) -> None:
        self._node_url = node_url
        self.lookups = 0

    def planned_node_url(self, identity: Any) -> str | None:
        self.lookups += 1
        return self._node_url


def _placement(*, reader) -> NodePlacement:
    return NodePlacement(max_pending=_MAX_PENDING, load_reader=reader)


def _never_probed(nodes):  # pragma: no cover - the assertion IS the test
    raise AssertionError("a single-node pool must not be probed")


def test_a_retry_of_an_existing_intent_reads_node_url_and_never_chooses(
    monkeypatch,
) -> None:
    import app_shared.jobs.node_load as node_load_module

    placement = _placement(reader=_never_probed)
    intents = _RecordingIntents("http://scrapers-browser-3:6800")

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a retry must not re-run choose_node")

    monkeypatch.setattr(node_load_module, "choose_node", _explode)
    monkeypatch.setattr(node_load_module, "select_node", _explode)

    chosen = placement.place(
        domain="amazon.sa", nodes=_NODES, intents=intents, identity=object()
    )

    assert chosen == "http://scrapers-browser-3:6800"
    assert intents.lookups == 1


def test_a_new_batch_with_no_intent_row_does_choose() -> None:
    placement = _placement(reader=lambda nodes: _idle())

    chosen = placement.place(
        domain="amazon.sa", nodes=_NODES, intents=_RecordingIntents(None), identity=object()
    )

    assert chosen in _NODES


def test_a_single_node_pool_short_circuits_to_select_node() -> None:
    """`select_node` is kept for exactly this case — and probes nothing."""
    single = ["http://scrapers-browser-1:6800"]
    placement = _placement(reader=_never_probed)

    assert (
        placement.place(
            domain="amazon.sa",
            nodes=single,
            intents=_RecordingIntents(None),
            identity=object(),
        )
        == single[0]
    )


def test_a_saturated_pool_defers_the_batch_in_the_dispatch_path() -> None:
    saturated = {node: NodeLoad(running=1, pending=_MAX_PENDING) for node in _NODES}
    placement = _placement(reader=lambda nodes: saturated)

    assert (
        placement.place(
            domain="amazon.sa",
            nodes=_NODES,
            intents=_RecordingIntents(None),
            identity=object(),
        )
        is None
    )
    assert placement.deferred == 1


# --- 5. an unreachable node is saturated, for 60 s ---------------------------


def test_an_unreachable_node_is_never_chosen() -> None:
    loads = _idle()
    loads[_NODES[1]] = UNREACHABLE

    placed = {
        choose_node(f"shop-{i}.example.com", _NODES, loads=loads, max_pending=_MAX_PENDING)
        for i in range(40)
    }

    assert _NODES[1] not in placed


def test_unreachable_is_not_the_same_as_idle() -> None:
    """`reachable=False` outranks any depth — it is absence of evidence."""
    loads = {node: NodeLoad(running=3, pending=3) for node in _NODES}
    loads[_NODES[0]] = UNREACHABLE

    assert (
        choose_node("amazon.sa", _NODES, loads=loads, max_pending=8) != _NODES[0]
    )


def test_every_node_unreachable_returns_none() -> None:
    loads = {node: UNREACHABLE for node in _NODES}

    assert choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING) is None


def test_daemon_status_none_produces_an_unreachable_load_held_for_60s() -> None:
    class _DeadNode:
        def daemon_status(self, node_url: str) -> None:
            return None

    writes: dict[str, tuple[str, int]] = {}

    class _Redis:
        def get(self, name: str) -> str | None:
            return None

        def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None):
            writes[name] = (value, ex)
            return True

    loads = read_node_loads(_DeadNode(), _NODES, _Redis(), ttl=10)

    assert all(load == UNREACHABLE for load in loads.values())
    assert choose_node("amazon.sa", _NODES, loads=loads, max_pending=_MAX_PENDING) is None
    assert UNREACHABLE_TTL_SECONDS == 60
    assert {ex for _, ex in writes.values()} == {60}


def test_a_totally_unreachable_pool_defers_on_the_dispatch_path() -> None:
    """Nothing has started, so there is nothing to rescue: defer."""
    dead = {node: UNREACHABLE for node in _NODES}
    placement = _placement(reader=lambda nodes: dead)

    assert (
        placement.place(
            domain="amazon.sa",
            nodes=_NODES,
            intents=_RecordingIntents(None),
            identity=object(),
        )
        is None
    )


def test_a_totally_unreachable_pool_still_re_posts_on_the_recovery_path() -> None:
    """A dead pool is the stall reaper's precondition, not a capacity answer.

    `recover_stalled_batches` builds its placement with
    `unreachable_pool_fallback=True` (F-2: "every node this suite reaps
    is DEAD"). Deferring there would let a dead pool permanently suppress
    the one sweep that exists to move work off dead nodes.
    """
    dead = {node: UNREACHABLE for node in _NODES}
    placement = NodePlacement(
        max_pending=_MAX_PENDING,
        load_reader=lambda nodes: dead,
        unreachable_pool_fallback=True,
    )

    chosen = placement.place(
        domain="amazon.sa",
        nodes=_NODES,
        intents=_RecordingIntents(None),
        identity=object(),
    )

    assert chosen == select_node("amazon.sa", _NODES)
    assert placement.deferred == 0


def test_the_recovery_fallback_does_not_apply_to_a_reachable_but_full_pool() -> None:
    full = {node: NodeLoad(running=1, pending=_MAX_PENDING) for node in _NODES}
    placement = NodePlacement(
        max_pending=_MAX_PENDING,
        load_reader=lambda nodes: full,
        unreachable_pool_fallback=True,
    )

    assert (
        placement.place(
            domain="amazon.sa",
            nodes=_NODES,
            intents=_RecordingIntents(None),
            identity=object(),
        )
        is None
    )
    assert placement.deferred == 1
