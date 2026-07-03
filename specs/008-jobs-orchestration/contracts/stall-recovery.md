# Contract: stalled-batch recovery (`recover_stalled_batches`, maintenance task)

Detect a batch dispatched to a node that died with the batch still queued, and re-dispatch it past a configured timeout — under the same idempotency guard and without double-running progressed targets (FR-015, US3-AS3, §26).

## `recover_stalled_batches()` (queue `maintenance`)

1. Scan **non-terminal** jobs (`status in (RUNNING,)`, with `started_at` set).
2. For each, select its targets still in a **non-progressed** state — `status == PENDING` (never moved to STARTED) — whose stall age exceeds `SCRAPE_STALL_TIMEOUT_SECONDS` (default 900 s), measured from the job's `started_at`.
3. Exclude targets that have progressed (`STARTED`/terminal) and, where SPEC-11 in-flight locks exist, targets with a live `locked_at` — so recovery never re-runs progressed or in-flight matches.
4. Re-resolve each stalled target's `competitor_domain` + `mode` set-based (same one-read pattern as dispatch, not per-target — U3); `re_batches = plan_batches(stalled_targets, ...)`; re-dispatch each via `client.schedule(..., batch_index=f"{batch_index}:r{stall_window(now, timeout)}", node_url=select_node(domain, nodes))` where `nodes` is the mode-appropriate pool (`SCRAPYD_BROWSER_URLS` for BROWSER else `SCRAPYD_HTTP_URLS`, I1).

## Idempotency of recovery itself

- The `batch_index` suffix `:r{stall_window}` differs from the original dispatch key, so the stalled batch actually re-dispatches (the original `dispatched:{job}:{i}` key is not reused/wedged).
- Within one stall window, a duplicated `recover_stalled_batches` delivery produces the **same** suffixed key → the `SET NX` guard neutralizes the duplicate (no double re-dispatch). The next window mints a fresh key, allowing a genuine later retry if still stalled.
- Node selection stays deterministic hash-by-domain (FR-014).

## Scheduling

- The task is invocable + idempotent now; **periodic** invocation (celery beat / the custom scheduler) is wired by the scheduler spec (SPEC-13), consistent with how SPEC-07 shipped the dispatch task without a running scheduler.

## Tests (`test_jobs_stall_recovery.py`, fakes)

- Targets still PENDING past the timeout → re-dispatched; targets STARTED/terminal or `locked_at`-live → excluded.
- Within a window, duplicate recovery delivery → one re-dispatch (SET NX no-op on the second); across windows → a fresh key permits re-dispatch.
- Same domain → same node on the re-dispatch.
