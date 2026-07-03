"""Deterministic node selection (`contracts/node-selection.md`, D3, FR-014).

Pure function — no state, no persistence. The dispatch task picks the
mode-appropriate Scrapyd node pool (`Settings.SCRAPYD_HTTP_URLS` for HTTP
batches, `Settings.SCRAPYD_BROWSER_URLS` for BROWSER batches, I1) and
passes it here; :func:`select_node` never reads config itself.
"""

from __future__ import annotations

import hashlib

__all__ = ["select_node"]


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
    """
    if not nodes:
        raise ValueError("select_node requires a non-empty node pool")
    return nodes[_stable_hash(domain) % len(nodes)]
