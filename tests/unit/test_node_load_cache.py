"""`read_node_loads` Redis cache unit tests (EPA B6 / F11).

`app_shared.jobs.node_load.read_node_loads` is the only I/O half of B6's
placement rule, and it has exactly three jobs:

* turn `daemonstatus.json` into `NodeLoad(running, pending)`;
* probe each node at most once per `DEFAULT_TTL_SECONDS` (10) by caching
  the answer in Redis, fleet-wide, one key per node;
* cache a node that did NOT answer for `UNREACHABLE_TTL_SECONDS` (60) as
  `reachable=False` — B6's "an unreachable node is treated as saturated
  for 60 s". The negative TTL is deliberately the LONGER of the two: a
  dead node must not be re-probed (and re-timed-out) by every pass.

Redis is a cache and nothing else here: a Redis that errors on read or
write degrades to "probe every time", never to "invent capacity".
"""

from __future__ import annotations

import json

from app_shared.jobs.node_load import (
    DEFAULT_TTL_SECONDS,
    UNREACHABLE_TTL_SECONDS,
    node_load_key,
    read_node_loads,
)
from app_shared.jobs.nodes import UNREACHABLE, NodeLoad

_NODES = [
    "http://scrapers-browser-1:6800",
    "http://scrapers-browser-2:6800",
]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}
        self.gets = 0
        self.sets = 0

    def get(self, name: str) -> str | None:
        self.gets += 1
        return self.store.get(name)

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None):
        self.sets += 1
        if nx and name in self.store:
            return None
        self.store[name] = value
        self.expiries[name] = ex
        return True


class FakeNode:
    """A Scrapyd node with a scripted `daemonstatus.json`."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self._payloads = payloads
        self.probes: list[str] = []

    def daemon_status(self, node_url: str):
        self.probes.append(node_url)
        return self._payloads.get(node_url)


class ExplodingRedis:
    def get(self, name: str):
        raise RuntimeError("redis down")

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None):
        raise RuntimeError("redis down")


def _healthy(running: int = 0, pending: int = 0) -> dict[str, int]:
    return {"node_name": "scrapers", "running": running, "pending": pending, "finished": 7}


# --- the happy path ----------------------------------------------------------


def test_reads_running_and_pending_off_daemon_status() -> None:
    node = FakeNode({_NODES[0]: _healthy(2, 3), _NODES[1]: _healthy(0, 1)})

    loads = read_node_loads(node, _NODES, FakeRedis())

    assert loads[_NODES[0]] == NodeLoad(running=2, pending=3, reachable=True)
    assert loads[_NODES[1]] == NodeLoad(running=0, pending=1, reachable=True)
    assert loads[_NODES[0]].depth == 5


def test_every_requested_node_is_present_in_the_result() -> None:
    """`choose_node` must never have to tell 'absent' from 'unreachable'."""
    node = FakeNode({_NODES[0]: _healthy(1, 1)})  # node 2 answers nothing

    loads = read_node_loads(node, _NODES, FakeRedis())

    assert set(loads) == set(_NODES)
    assert loads[_NODES[1]] == UNREACHABLE


def test_a_duplicated_node_is_probed_once() -> None:
    node = FakeNode({_NODES[0]: _healthy(1, 1)})

    read_node_loads(node, [_NODES[0], _NODES[0], _NODES[0]], FakeRedis())

    assert node.probes == [_NODES[0]]


# --- the cache ---------------------------------------------------------------


def test_a_second_call_within_the_ttl_does_not_probe_again() -> None:
    redis = FakeRedis()
    node = FakeNode({_NODES[0]: _healthy(1, 2), _NODES[1]: _healthy(0, 0)})

    first = read_node_loads(node, _NODES, redis)
    probes_after_first = list(node.probes)
    second = read_node_loads(node, _NODES, redis)

    assert node.probes == probes_after_first, "the second pass reused the cache"
    assert second == first


def test_a_healthy_status_is_cached_for_ttl_seconds() -> None:
    redis = FakeRedis()
    node = FakeNode({_NODES[0]: _healthy(1, 2), _NODES[1]: _healthy(0, 0)})

    read_node_loads(node, _NODES, redis, ttl=DEFAULT_TTL_SECONDS)

    assert DEFAULT_TTL_SECONDS == 10
    for node_url in _NODES:
        assert redis.expiries[node_load_key(node_url)] == DEFAULT_TTL_SECONDS


def test_the_cache_key_is_one_per_node_and_carries_no_tenant() -> None:
    redis = FakeRedis()
    read_node_loads(FakeNode({}), _NODES, redis)

    assert set(redis.store) == {node_load_key(n) for n in _NODES}
    assert node_load_key("http://scrapers-browser-1:6800/") == node_load_key(
        "http://scrapers-browser-1:6800"
    )


def test_a_cached_entry_round_trips_through_json() -> None:
    redis = FakeRedis()
    redis.store[node_load_key(_NODES[0])] = json.dumps(
        {"running": 4, "pending": 2, "reachable": True}
    )
    node = FakeNode({_NODES[0]: _healthy(0, 0)})

    loads = read_node_loads(node, [_NODES[0]], redis)

    assert loads[_NODES[0]] == NodeLoad(running=4, pending=2, reachable=True)
    assert node.probes == [], "a cache hit never probes"


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash() -> None:
    redis = FakeRedis()
    redis.store[node_load_key(_NODES[0])] = "}{ not json"
    node = FakeNode({_NODES[0]: _healthy(1, 1)})

    loads = read_node_loads(node, [_NODES[0]], redis)

    assert loads[_NODES[0]] == NodeLoad(running=1, pending=1, reachable=True)
    assert node.probes == [_NODES[0]]


# --- the negative cache: unreachable == saturated, for 60 s ------------------


def test_an_unreachable_node_is_cached_unreachable_for_60_seconds() -> None:
    redis = FakeRedis()
    node = FakeNode({})  # daemon_status -> None for everything

    loads = read_node_loads(node, _NODES, redis, ttl=DEFAULT_TTL_SECONDS)

    assert all(load == UNREACHABLE for load in loads.values())
    assert UNREACHABLE_TTL_SECONDS == 60
    for node_url in _NODES:
        assert redis.expiries[node_load_key(node_url)] == UNREACHABLE_TTL_SECONDS


def test_the_negative_ttl_ignores_the_callers_ttl_argument() -> None:
    """60 s is a property of B6's placement rule, not a tuning knob."""
    redis = FakeRedis()

    read_node_loads(FakeNode({}), [_NODES[0]], redis, ttl=1)

    assert redis.expiries[node_load_key(_NODES[0])] == UNREACHABLE_TTL_SECONDS


def test_an_unreachable_node_is_not_re_probed_while_the_entry_lives() -> None:
    redis = FakeRedis()
    node = FakeNode({})

    read_node_loads(node, _NODES, redis)
    read_node_loads(node, _NODES, redis)
    read_node_loads(node, _NODES, redis)

    assert node.probes == list(_NODES), "one probe per dead node, not three"


def test_a_malformed_payload_is_unreachable_not_idle() -> None:
    """`{"pending": null}` is not evidence that a node is free."""
    node = FakeNode(
        {
            _NODES[0]: {"running": 0, "pending": None},
            _NODES[1]: {"running": "many", "pending": 0},
        }
    )

    loads = read_node_loads(node, _NODES, FakeRedis())

    assert loads[_NODES[0]] == UNREACHABLE
    assert loads[_NODES[1]] == UNREACHABLE


def test_a_non_dict_payload_is_unreachable() -> None:
    node = FakeNode({_NODES[0]: ["running", 1]})

    assert read_node_loads(node, [_NODES[0]], FakeRedis())[_NODES[0]] == UNREACHABLE


# --- Redis is a cache, never a dependency ------------------------------------


def test_a_redis_outage_degrades_to_probing_every_time() -> None:
    node = FakeNode({_NODES[0]: _healthy(1, 1)})

    first = read_node_loads(node, [_NODES[0]], ExplodingRedis())
    second = read_node_loads(node, [_NODES[0]], ExplodingRedis())

    assert first == second == {_NODES[0]: NodeLoad(running=1, pending=1)}
    assert node.probes == [_NODES[0], _NODES[0]]


def test_no_redis_at_all_still_reads_loads() -> None:
    node = FakeNode({_NODES[0]: _healthy(3, 0)})

    assert read_node_loads(node, [_NODES[0]], None)[_NODES[0]] == NodeLoad(running=3)
