# Contract: deterministic node selection (`app_shared.jobs.nodes`)

Pure function — no state, no persistence.

## `select_node(domain, nodes) -> node_url`

- `nodes`: the ordered, non-empty pool for the batch's mode — `Settings.SCRAPYD_HTTP_URLS` for HTTP batches, `Settings.SCRAPYD_BROWSER_URLS` for BROWSER batches (caller picks the pool by `batch.mode`; I1).
- Returns `nodes[stable_hash(domain) % len(nodes)]` where `stable_hash` is a **process-stable** digest (e.g. `int.from_bytes(hashlib.blake2b(domain.encode(), digest_size=8).digest())`) — **not** Python's builtin `hash()` (salted per process via `PYTHONHASHSEED`, so non-deterministic across workers).
- Single-node pool → always that node.

## Guarantee (FR-014, US3-AS4)

- The same `domain` always maps to the same node, in any worker process, across dispatch retries — so two retries of one batch can never send it to two different nodes.

## Tests (`test_jobs_node_selection.py`)

- Determinism: repeated calls (and a fresh interpreter / reimport) for the same domain → same node.
- Distribution: different domains spread across a multi-node pool.
- Single-node pool → that node.
