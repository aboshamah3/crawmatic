# Contract: batch planning (`app_shared.jobs.batching`)

Pure function — no DB/Redis/network, no scrapy/twisted. Unit-testable against in-memory target/match rows.

## `plan_batches(targets, *, http_min=50, http_max=200) -> list[Batch]`

- Input: the job's targets plus each target's resolved `competitor_domain` + `mode` (`ScrapeProfileMode`). (The dispatch task resolves domain/mode set-based from the matches/competitors; `plan_batches` receives them attached — it does not query.)
- Group targets by `(competitor_domain, mode)` (§27 grouping: workspace is implicit — one job is one workspace).
- Chunk each group into batches of at most `http_max` (default 200) match_ids; small groups form a single batch; the 50–200 guidance (`http_min`/`http_max`) bounds HTTP batches (SC-008, FR-011). Browser batches (5–25) are a later-spec mode; this spec dispatches HTTP.
- `batch_index` is the stable enumerated position over a **canonical sort** of the groups (e.g. sorted by `(domain, mode, chunk_ordinal)`) so re-planning the same targets yields the same indices (supports dispatch idempotency + deterministic node selection).
- Output: `list[Batch]` where `Batch = (batch_index: int, mode: ScrapeProfileMode, domain: str, match_ids: list[UUID])`.

## Invariants

- Every input target appears in exactly **one** batch; no match is duplicated across batches (complements `unique(scrape_job_id, match_id)` — the orchestration layer never schedules the same match twice in one batch, edge case "concurrent runs of the same match").
- Empty input → empty list (no batches).
- No batch exceeds `http_max`.

## Tests (`test_jobs_batching.py`)

- Multi-domain / multi-mode targets → grouped by domain+mode; a >`http_max` group splits into ≤`http_max` chunks; batch sizes within [1, `http_max`] and honoring the 50–200 guidance where the group allows.
- Stable `batch_index` across repeated calls on the same input.
- No match in two batches; empty input → no batches.
