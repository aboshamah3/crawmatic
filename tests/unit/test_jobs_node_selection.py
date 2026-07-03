"""Deterministic node selection unit tests (SPEC-08 T028, US1, FR-014, SC-005).

`app_shared.jobs.nodes.select_node` — pure, no state/persistence. Per
`contracts/node-selection.md`: the same domain always maps to the same
node (across repeated calls AND a fresh process/interpreter, since
Python's builtin `hash()` is per-process salted and must NOT be used);
different domains spread across a multi-node pool; a single-node pool
always returns that node.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app_shared.jobs.nodes import select_node

_NODES = ["http://scraper-a:6800", "http://scraper-b:6800", "http://scraper-c:6800"]


def test_same_domain_maps_to_same_node_across_repeated_calls() -> None:
    node_1 = select_node("shop.example.com", _NODES)
    node_2 = select_node("shop.example.com", _NODES)

    assert node_1 == node_2
    assert node_1 in _NODES


def test_single_node_pool_always_returns_that_node() -> None:
    single = ["http://only-scraper:6800"]

    assert select_node("shop.example.com", single) == single[0]
    assert select_node("another.example.com", single) == single[0]


def test_different_domains_distribute_across_a_multi_node_pool() -> None:
    domains = [f"shop-{i}.example.com" for i in range(50)]
    chosen = {select_node(domain, _NODES) for domain in domains}

    # With 50 distinct domains hashed over a 3-node pool, every node
    # should be selected at least once (not a degenerate always-node-0
    # mapping).
    assert chosen == set(_NODES)


def test_select_node_raises_on_empty_pool() -> None:
    with pytest.raises(ValueError):
        select_node("shop.example.com", [])


# --- cross-process determinism (not Python's salted builtin hash()) --------

_CROSS_PROCESS_CHECK = """
import sys
from app_shared.jobs.nodes import select_node

nodes = {nodes!r}
print(select_node("shop.example.com", nodes))
"""


def test_same_domain_maps_to_same_node_across_a_fresh_process() -> None:
    """`PYTHONHASHSEED` is randomized per process by default; builtin
    `hash()` would therefore vary the result across processes. This
    proves `select_node` does NOT (it uses a process-stable digest)."""
    in_process = select_node("shop.example.com", _NODES)

    code = _CROSS_PROCESS_CHECK.format(nodes=_NODES)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == in_process
